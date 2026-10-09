"""walkingpad-client — CLI front end for the walkingpad daemon.

Talks JSON lines over the daemon's Unix socket, prints one JSON reply per
command, and exits non-zero when the daemon reports an error, so it can be
scripted or bound to hotkeys. Pure standard library — no Python packages.
"""

import argparse
import asyncio
import collections
import json
import sys

DEFAULT_SOCKET = "/run/walkingpad/daemon.sock"
DEFAULT_HISTORY = "/var/lib/walkingpad/history.jsonl"
DEFAULT_CURRENT = "/var/lib/walkingpad/current.json"


def fmt_duration(total):
    m, s = divmod(int(total), 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s)


def cmd_history(args):
    """Per-day totals from the daemon's session history log.

    The daemon appends one JSON record per finalized session (reason
    "stop"); any other reason is shown only with --all. An open or
    unresolved session (current.json) is shown separately and not
    counted.
    """
    days = collections.OrderedDict()
    try:
        fh = open(args.file, encoding="utf-8")
    except FileNotFoundError:
        print("walkingpad-client: no history yet (%s)" % args.file,
              file=sys.stderr)
        return 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("reason") != "stop" and not args.all:
                continue
            day = rec.get("date") or "unknown"
            agg = days.setdefault(
                day, {"distance": 0, "duration": 0, "steps": 0, "sessions": 0})
            agg["distance"] += rec.get("distance") or 0
            agg["duration"] += rec.get("duration") or 0
            agg["steps"] += rec.get("steps") or 0
            agg["sessions"] += 1
    rows = list(days.items())
    if args.days:
        rows = rows[-args.days:]
    for day, agg in rows:
        print("%s  %6.2f km  %s  %6d steps  (%d session%s)"
              % (day, agg["distance"] / 1000.0, fmt_duration(agg["duration"]),
                 agg["steps"], agg["sessions"],
                 "" if agg["sessions"] == 1 else "s"))
    try:
        with open(args.current, encoding="utf-8") as fh:
            pending = json.load(fh)
    except (OSError, ValueError):
        return 0
    sess = pending.get("session") or {}
    print("open session: %6.2f km  %s  %d steps  (not yet counted)"
          % ((sess.get("distance") or 0) / 1000.0,
             fmt_duration(sess.get("duration") or 0),
             sess.get("steps") or 0))
    return 0


async def round_trip(socket_path, request, watch=False):
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise RuntimeError("daemon closed the connection")
        reply = json.loads(line)
        while watch:
            # The daemon pushes one JSON state line per update until this
            # side hangs up.
            line = await reader.readline()
            if not line:
                break
            print(line.decode().strip(), flush=True)
        return reply
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def build_request(args):
    if args.command == "ping":
        return {"cmd": "ping"}, False
    if args.command == "status":
        return {"cmd": "status"}, False
    if args.command == "watch":
        return {"cmd": "watch"}, True
    if args.command == "properties":
        return {"cmd": "properties"}, False
    if args.command == "start":
        return {"cmd": "start", "speed": args.speed}, False
    if args.command == "speed":
        return {"cmd": "speed", "speed": args.speed}, False
    if args.command == "stop":
        return {"cmd": "stop"}, False
    if args.command == "pause":
        return {"cmd": "pause"}, False
    if args.command == "set-buzzer":
        return {"cmd": "set-buzzer", "value": args.value}, False
    raise AssertionError(args.command)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="walkingpad-client",
        description="Control the walkingpad daemon (persistent WalkingPad link)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help="path of the daemon control socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check that the daemon answers")
    sub.add_parser("status", help="connection state and last telemetry")
    sub.add_parser("watch", help="stream state updates until interrupted")
    sub.add_parser("properties", help="dump vendor property records")
    p_start = sub.add_parser("start", help="start the belt")
    p_start.add_argument("--speed", type=float, help="target speed in km/h")
    p_speed = sub.add_parser("speed", help="set speed (starts belt if stopped)")
    p_speed.add_argument("speed", type=float, help="speed in km/h")
    sub.add_parser("stop", help="stop the belt (finalizes the pad session)")
    sub.add_parser("pause", help="pause the belt (keeps the session open)")
    p_buzzer = sub.add_parser("set-buzzer", help="toggle the pad's buzzer bit")
    p_buzzer.add_argument("value", choices=["on", "off"])
    p_hist = sub.add_parser("history", help="per-day distance/duration/steps totals")
    p_hist.add_argument("--file", default=DEFAULT_HISTORY,
                        help="path of the daemon's history log")
    p_hist.add_argument("--current", default=DEFAULT_CURRENT,
                        help="path of the daemon's open-session snapshot")
    p_hist.add_argument("--days", type=int, default=0,
                        help="only show the newest N days (0 = all)")
    p_hist.add_argument("--all", action="store_true",
                        help="also count pause/disconnect records "
                             "(overlaps sessions; only for inspection)")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "history":
        sys.exit(cmd_history(args))
    request, watch = build_request(args)
    try:
        reply = asyncio.run(round_trip(args.socket, request, watch))
    except (OSError, RuntimeError) as exc:
        print("walkingpad-client: %s" % exc, file=sys.stderr)
        sys.exit(2)
    if not watch:
        print(json.dumps(reply, indent=2))
    sys.exit(0 if reply.get("ok") else 1)


if __name__ == "__main__":
    main()
