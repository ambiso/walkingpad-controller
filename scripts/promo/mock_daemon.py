"""Private JSON-lines demo daemon. No Bluetooth or production socket access."""
import datetime
import json
import socketserver
import threading
import time


class DemoDaemon:
    def __init__(self, socket):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.closed = threading.Event()
        self.generation = 0
        self.growth = None
        self.elapsed = self.steps = 0.0
        self.state = dict(connected=True, belt="stopped", starting=False,
                          speed=0.0, min_speed=1.0, max_speed=6.0,
                          distance=0, elapsed=0, steps=0, history_revision=0)
        today = datetime.date.today()
        self.history = [dict(date=str(today - datetime.timedelta(days=6 - i)),
                             reason="stop", steps=steps, distance=round(steps * 0.68),
                             duration=round(steps / 1.6))
                        for i, steps in enumerate([4200, 7100, 5300, 8600, 6100, 9400, 2300])]
        self.target_today_steps = 6 * max(record["steps"] for record in self.history)
        self.target_session_steps = self.target_today_steps - self.history[-1]["steps"]
        demo = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    for line in self.rfile:
                        request = json.loads(line)
                        if request["cmd"] == "watch":
                            generation = -1
                            while not demo.closed.is_set():
                                with demo.changed:
                                    demo.changed.wait_for(lambda: generation != demo.generation or demo.closed.is_set(), 1)
                                    generation = demo.generation
                                    reply = dict(ok=True, state=demo.state.copy())
                                self.wfile.write((json.dumps(reply) + "\n").encode())
                                self.wfile.flush()
                            return
                        reply = demo.command(request)
                        self.wfile.write((json.dumps(reply) + "\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(socket), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.tick, daemon=True).start()

    def publish(self):
        self.generation += 1
        self.changed.notify_all()

    def command(self, request):
        with self.changed:
            cmd = request["cmd"]
            if cmd == "history":
                current = None
                if self.state["elapsed"]:
                    current = dict(date=str(datetime.date.today()), session={
                        "distance": self.state["distance"], "duration": self.state["elapsed"],
                        "steps": self.state["steps"]})
                return dict(ok=True, history=list(self.history), current=current,
                            history_revision=self.state["history_revision"])
            if cmd == "start":
                self.state.update(starting=True, speed=request["speed"], elapsed=0, steps=0, distance=0)
                self.start_at = time.monotonic() + 3.3
                self.growth = None
                self.elapsed = self.steps = 0.0
            elif cmd == "speed":
                self.state["speed"] = request["speed"]
            elif cmd == "stop":
                self.history.append(dict(date=str(datetime.date.today()), reason="stop",
                                         distance=self.state["distance"], duration=self.state["elapsed"],
                                         steps=self.state["steps"]))
                self.state.update(belt="stopped", starting=False, speed=0.0,
                                  history_revision=self.state["history_revision"] + 1,
                                  elapsed=0, steps=0, distance=0)
            else:
                return dict(ok=False, error=f"Unsupported demo command: {cmd}")
            self.publish()
            return dict(ok=True, state=self.state.copy())

    def grow_session(self):
        """Ease to six times the largest sample day at 60 Hz for three seconds."""
        with self.changed:
            self.growth = (time.monotonic(), self.elapsed, self.steps, self.state["distance"])
            self.changed.notify_all()

    def tick(self):
        previous = time.monotonic()
        while not self.closed.is_set():
            with self.changed:
                animating = self.growth is not None and time.monotonic() - self.growth[0] < 3
                self.changed.wait(1 / 60 if animating else 1)
                if self.closed.is_set():
                    return
                now = time.monotonic()
                delta = now - previous
                previous = now
                if self.state["starting"] and now >= self.start_at:
                    self.state.update(starting=False, belt="started")
                if self.state["belt"] == "started":
                    rate = 1.15 + self.state["speed"] * 0.12
                    if self.growth is None:
                        self.elapsed += delta
                        self.steps += delta * rate
                        self.state["distance"] += self.state["speed"] * delta / 3.6
                    else:
                        began, elapsed, steps, distance = self.growth
                        progress = min(1, (now - began) / 3)
                        eased = progress * progress * (3 - 2 * progress)
                        gained = (self.target_session_steps - steps) * eased
                        self.steps = self.target_session_steps if progress == 1 else steps + gained
                        self.elapsed = elapsed + gained / rate
                        self.state["distance"] = distance + self.state["speed"] * gained / rate / 3.6
                    self.state["elapsed"] = int(self.elapsed)
                    self.state["steps"] = int(self.steps)
                    self.publish()

    def close(self):
        self.closed.set()
        with self.changed:
            self.publish()
        self.server.shutdown()
        self.server.server_close()
