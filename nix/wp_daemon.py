"""walkingpad-daemon — persistent control daemon for KingSmith WalkingPad
Z1-series belts (BLE names "KS-HD-*").

Holds the pad's single BLE link for the lifetime of the service — so the
connect/disconnect beeps happen once per service start, not once per
command — and serves a JSON-lines control socket for walkingpad-client.

Why not the walkingpad-controller library: Z1-series firmware gates all
FTMS control behind a vendor unlock handshake on the KingSmith supplement
service. The library never sends it, so its REQUEST_CONTROL is silently
ignored and later commands answer result 5 "control not permitted" while
the belt keeps running. This daemon speaks the wire protocol itself:

  1. subscribe the supplement notify characteristic (before any write)
  2. unlock: write 71 00 05 01 <LE32(name[-4:])+1> CC, await 71 80
  3. FTMS control point 0x2AD9 (write WITH response, >=400 ms pacing):
     00 request control, 07 start, 02 <u16 LE> speed, 08 01 stop, 08 02 pause
  4. telemetry 0x2ACD (flags u16 LE; bit0 clear = speed u16 km/h*100,
     bit2 = distance u24 m, bit10 = elapsed u16 s, bit13 = steps u16,
     a KingSmith extension); machine status 0x2ADA (04 started, 02 user
     stop, 01 safety stop, 05 speed changed, FF control lost)

Vendor frames: [cmd0, cmd1, len, data..., checksum], checksum =
sum(all prior bytes) & 0xFF, written WITHOUT response, >=400 ms apart.
Never send frames starting with 0xE8 (OTA mode — brick risk).

Protocol sources: slandau3/z1-walkingpad-mcp docs/protocol.md (MIT),
anativ/R1-Walkingpad-Controller, duttke.de Web Bluetooth implementation.
"""

import argparse
import asyncio
import json
import os
import sys
import time

from bleak import BleakClient, BleakScanner

FTMS_CP = "00002ad9-0000-1000-8000-00805f9b34fb"
FTMS_DATA = "00002acd-0000-1000-8000-00805f9b34fb"
FTMS_STATUS = "00002ada-0000-1000-8000-00805f9b34fb"
FTMS_SPEED_RANGE = "00002ad4-0000-1000-8000-00805f9b34fb"
FIRMWARE_REV = "00002a26-0000-1000-8000-00805f9b34fb"

KS_NOTIFY = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
KS_WRITE = "24e2521c-f63b-48ed-85be-c5330d00fdf7"

OP_REQUEST_CONTROL = 0x00
OP_SET_SPEED = 0x02
OP_START = 0x07
OP_STOP_PAUSE = 0x08

MACHINE_STATUS = {
    0x01: "safety-stop",
    0x02: "stopped",
    0x04: "started",
    0x05: "speed-changed",
    0xFF: "control-lost",
}

RESULT_TEXT = {
    1: "success",
    2: "op not supported",
    3: "invalid parameter",
    4: "failed",
    5: "control not permitted",
}

WRITE_PACE = 0.4  # both channels silently drop writes closer than 400 ms

FLUSH_INTERVAL = 5.0  # min seconds between current.json rewrites

# Session stats are accumulated as deltas between consecutive telemetry
# frames (~1 s apart while the belt runs), in the pad's own counter
# widths. A decrease modulo the counter width is either a counter wrap
# (tiny true delta) or a session finalize (delta near the full counter
# range); these thresholds — far beyond anything walkable between two
# frames — separate them once, for every counter.
PAD_TO_RECORD = {
    "distance": ("distance", 0xFFFFFF),  # u24 meters
    "elapsed": ("duration", 0xFFFF),      # u16 seconds of walking
    "steps": ("steps", 0xFFFF),           # u16 steps
}
RESET_THRESHOLD = {"distance": 50000, "duration": 3600, "steps": 50000}


def ks_frame(cmd0, cmd1, data):
    # Vendor frame: [cmd0, cmd1, len, data..., checksum]; the checksum is the
    # byte sum of everything before it, truncated to one byte.
    body = bytes([cmd0, cmd1, len(data)]) + bytes(data)
    return body + bytes([sum(body) & 0xFF])


def unlock_frame(name):
    # T = LE32(last 4 chars of the BLE name) + 1. For KS-HD-Z1D the last four
    # chars are "-Z1D" = 2d 5a 31 44, +1 = 2e 5a 31 44, so the frame is
    # 71 00 05 01 2e 5a 31 44 74 — exactly the documented example.
    token = int.from_bytes(name[-4:].encode("ascii"), "little") + 1
    return ks_frame(0x71, 0x00, [0x01] + list(token.to_bytes(4, "little")))


async def find_device(address=None, prefix="KS-", timeout=10.0):
    # Same discovery logic as wp_cli.py: pin by address if given, else match
    # the advertised name prefix (KS-HD-Z1E matches the default "KS-").
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def match(device, _adv):
        return device.name is not None and device.name.startswith(prefix)

    return await BleakScanner.find_device_by_filter(match, timeout=timeout)


async def bluez_force_disconnect(address):
    # The pad does not advertise while a GATT connection exists, and
    # bleak cannot connect to a bare address that is not advertising, so
    # a stale connection bluetoothd never tore down blocks discovery
    # forever. This is the org.bluez.Device1 Disconnect call (what
    # bluetoothctl does), made over the system bus so the daemon can
    # clear its own leaked links. Best-effort: returns True only when a
    # connected device with that address was found and disconnected.
    try:
        from dbus_fast import BusType, Message
        from dbus_fast.aio import MessageBus
    except ImportError:
        return False
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects"))
        objects = reply.body[0] if reply.body else {}
        for path, interfaces in objects.items():
            props = (interfaces or {}).get("org.bluez.Device1")
            if not props or props.get("Address") != address:
                continue
            if not props.get("Connected"):
                continue
            await bus.call(Message(
                destination="org.bluez", path=path,
                interface="org.bluez.Device1", member="Disconnect"))
            return True
    finally:
        bus.disconnect()
    return False


class Pad:
    """One persistent BLE link: unlock, control, telemetry, reconnect."""

    def __init__(self, args):
        self.address = args.address
        self.prefix = args.prefix
        self.keepalive = args.keepalive
        self.reconnect_delay = args.reconnect_delay
        self.history_path = args.history
        self.history_dir = os.path.dirname(args.history) if args.history else ""
        self.current_path = (os.path.join(self.history_dir, "current.json")
                             if self.history_dir else "")
        self.history_failed = False
        self.pause_pending = False
        self.last_flush = 0.0
        # Session accounting: stats are accumulated per telemetry frame
        # (see _on_telemetry), never taken from the counters' absolute
        # values, so counter wraps and session finalizes need no special
        # cases of their own.
        self.session = None          # {distance, duration, steps} or None
        self.prev_counters = {}      # last raw telemetry values per field
        # A leftover current.json from an unclean shutdown (power loss,
        # systemd stop mid-session) is the accumulated session plus the
        # last raw counters seen. The first telemetry frame after startup
        # resolves it with the same delta rule as everything else.
        self.recovery = self._read_current()
        self.client = None
        self.vendor_lock = asyncio.Lock()
        self.vendor_reply = None
        self.cp_lock = asyncio.Lock()
        self.cp_reply = {}
        self.last_write = 0.0
        self.watchers = set()
        self.state = {
            "connected": False,
            "unlocked": False,
            "control": False,
            "starting": False,
            "belt": "unknown",
            "speed": 0.0,
            "distance": 0,
            "elapsed": 0,
            "steps": 0,
            "min_speed": None,
            "max_speed": None,
            "firmware": None,
            "device": None,
        }

    def _set(self, **changes):
        belt_before = self.state["belt"]
        # The pad counts down ~3 s between an accepted start and the belt
        # actually moving; any belt transition out of "unknown" ends that
        # window, whatever notification carries it.
        if self.state.get("starting") and changes.get("belt") is not None:
            changes["starting"] = False
        self.state.update(changes)
        # Entering "stopped" (machine-status frame: user stop, safety
        # stop, daemon command) finalizes the pad session. The previous
        # state can be "started" but also "unknown" — after a BLE
        # reconnect the belt is only known through telemetry until the
        # next status frame, and the stop frame must still close the
        # session. Entering "unknown" (link lost) does not close it: the
        # pad session most likely keeps running and the delta rule sorts
        # it out on reconnect.
        if (self.state["belt"] == "stopped" and belt_before != "stopped"):
            self._on_belt_stopped()
        if any(k in changes for k in ("distance", "elapsed", "steps")):
            self._on_telemetry(changes)
        for queue in self.watchers:
            queue.put_nowait(dict(self.state))

    # -- session accounting -------------------------------------------------
    #
    # Stats are accumulated as per-frame deltas of the pad's telemetry
    # counters, never taken from the counters' absolute values:
    #
    #     delta = (new - old) & counter_mask
    #
    # The pad pushes telemetry about once a second while the belt runs, so
    # consecutive frames differ by tiny amounts, which makes one modulo
    # rule handle every special case at once:
    #
    #  * a u16/u24 counter wrap: the wrapped remainder minus the old value
    #    modulo the counter width is the real per-frame increment — wraps
    #    disappear entirely, and even a ~18 h session records as one
    #    session with correct totals;
    #  * a session finalize and new session between two frames (stop via
    #    the handheld remote or the phone app racing past the
    #    machine-status frames, or the pad's trailing all-zero frame after
    #    a stop): (small - large) mod 2^n is near the full counter range —
    #    anything beyond RESET_THRESHOLD (an hour of walking between two
    #    consecutive frames cannot happen; the keepalive reconnects the
    #    link long before) closes the accumulated session and starts a
    #    fresh one from the incoming frame;
    #  * a BLE gap (reconnect): the delta is the genuinely walked amount
    #    and accumulates normally.
    #
    # The session is closed explicitly by the belt-stopped signal above
    # (unless the stop was a pause, which keeps the pad session open),
    # and {accumulated session, last raw counters} are flushed atomically
    # to current.json every FLUSH_INTERVAL seconds, so an unclean shutdown
    # loses at most that window. On startup the leftover snapshot resumes
    # under the same delta rule: a small delta means the same pad session
    # continued and the snapshot is adopted; a huge one means it was
    # finalized while the daemon was gone, and the snapshot is recorded
    # as a completed session.

    def _on_belt_stopped(self):
        if self.pause_pending:
            # A pause keeps the pad session open; accumulation simply
            # stops while the belt is idle, and the eventual stop record
            # carries the full totals.
            self.pause_pending = False
            return
        self._close_session()

    def _close_session(self):
        session = self.session
        self.session = None
        self.prev_counters = {}
        if session is None:
            return
        if session["distance"] == 0 and session["duration"] == 0:
            return  # belt never actually moved (start then instant stop)
        self._append_history({
            "ts": int(time.time()),
            "date": time.strftime("%Y-%m-%d"),
            "reason": "stop",
            **session,
        })
        try:
            os.unlink(self.current_path)
        except OSError:
            pass
        print("walkingpad: session recorded: %d m / %d s / %d steps"
              % (session["distance"], session["duration"], session["steps"]),
              file=sys.stderr)

    def _on_telemetry(self, changes):
        if not self.history_path or self.history_failed:
            return
        speed = changes.get("speed") or 0
        walking = speed > 0 or self.state["belt"] == "started"
        if self.session is None:
            if not walking:
                return
            if self.recovery is not None:
                # Resume the session that was open when the daemon last
                # died; the delta logic below decides against the
                # recovered counters whether it actually continued.
                snapshot = self.recovery
                self.recovery = None
                recovered = snapshot.get("session") or {
                    "distance": 0, "duration": 0, "steps": 0}
                self.session = dict(recovered)
                self.prev_counters = dict(snapshot.get("prev") or {})
            else:
                self.session = {"distance": 0, "duration": 0, "steps": 0}
                self.prev_counters = {}
        # One reset gate for the whole frame: any counter jumping by more
        # than RESET_THRESHOLD means the pad finalized the old session
        # (its counters reset) and is already reporting a new one.
        for pad_field, (rec_field, mask) in PAD_TO_RECORD.items():
            if pad_field in changes and pad_field in self.prev_counters:
                old = self.prev_counters[pad_field]
                delta = (changes[pad_field] - old) & mask
                if delta > RESET_THRESHOLD[rec_field]:
                    self._close_session()
                    self.session = {"distance": 0, "duration": 0, "steps": 0}
                    self.prev_counters = {}
                    break
        for pad_field, (rec_field, mask) in PAD_TO_RECORD.items():
            if pad_field not in changes:
                continue
            old = self.prev_counters.get(pad_field)
            if old is None:
                self.prev_counters[pad_field] = changes[pad_field]
                continue
            self.session[rec_field] += ((changes[pad_field] - old) & mask)
            self.prev_counters[pad_field] = changes[pad_field]
        self._maybe_flush_current()

    def read_history(self):
        # Served over the control socket (see dispatch): the state
        # directory belongs to the service's dynamic user and is not
        # reliably readable by other users.
        records = []
        try:
            with open(self.history_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return records

    def _append_history(self, record):
        try:
            os.makedirs(self.history_dir, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            self._disable_history(exc)

    def _disable_history(self, exc):
        # Unwritable history (e.g. running the daemon unprivileged with the
        # default path) must never take the pad link down.
        self.history_failed = True
        print("walkingpad: history disabled (%s)" % exc, file=sys.stderr)

    def _read_current(self):
        if not self.current_path or not os.path.exists(self.current_path):
            return None
        try:
            with open(self.current_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _write_current(self, record):
        # Atomic replace: a power cut mid-write must not corrupt the
        # snapshot that recovery depends on.
        tmp = self.current_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            os.replace(tmp, self.current_path)
        except OSError as exc:
            self._disable_history(exc)

    def _maybe_flush_current(self):
        if self.session is None or not self.current_path or self.history_failed:
            return
        now = time.monotonic()
        if now - self.last_flush < FLUSH_INTERVAL:
            return
        self.last_flush = now
        self._write_current({
            "ts": int(time.time()),
            "date": time.strftime("%Y-%m-%d"),
            "session": dict(self.session),
            "prev": dict(self.prev_counters),
        })

    async def _paced(self):
        # Enforce the 400 ms spacing that both the vendor channel and the
        # control point require; faster writes are dropped by the pad.
        wait = self.last_write + WRITE_PACE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_write = time.monotonic()

    async def _wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.1)
        return False

    # -- GATT notification handlers -------------------------------------

    def on_vendor(self, _char, data):
        frame = bytes(data)
        future = self.vendor_reply
        if future is not None and not future.done():
            future.set_result(frame)

    def on_cp(self, _char, data):
        # FTMS response indication: [0x80, request-op, result, params...]
        frame = bytes(data)
        if len(frame) >= 3 and frame[0] == 0x80:
            future = self.cp_reply.pop(frame[1], None)
            if future is not None and not future.done():
                future.set_result(frame[2])

    def on_status(self, _char, data):
        frame = bytes(data)
        if not frame:
            return
        status = MACHINE_STATUS.get(frame[0])
        if status == "started":
            self.pause_pending = False
            self._set(belt="started")
        elif status in ("stopped", "safety-stop"):
            self._set(belt="stopped", speed=0.0)
        elif status == "control-lost":
            self._set(control=False)

    def on_data(self, _char, data):
        # FTMS treadmill data: u16 LE flags, then fields in ascending flag-bit
        # order. The pad sends flags 0x2404 + speed; unknown-but-set bits are
        # consumed per the spec layout so later fields stay aligned.
        frame = bytes(data)
        if len(frame) < 2:
            return
        flags = int.from_bytes(frame[0:2], "little")
        offset = 2

        def take(size):
            nonlocal offset
            chunk = frame[offset:offset + size]
            offset += size
            return chunk if len(chunk) == size else None

        speed = distance = elapsed = steps = None
        if not flags & 0x0001:
            chunk = take(2)  # bit 0 clear: instantaneous speed, km/h * 100
            speed = int.from_bytes(chunk, "little") / 100.0 if chunk else None
        if flags & 0x0002:
            take(2)  # average speed
        if flags & 0x0004:
            chunk = take(3)  # total distance, meters
            distance = int.from_bytes(chunk, "little") if chunk else None
        if flags & 0x0008:
            take(4)  # inclination + ramp angle (s16 x2)
        if flags & 0x0010:
            take(4)  # positive/negative elevation gain (u16 x2)
        if flags & 0x0020:
            take(1)  # pace
        if flags & 0x0040:
            take(1)  # average pace
        if flags & 0x0080:
            take(5)  # energy: u16 + u16 + u8
        if flags & 0x0100:
            take(1)  # heart rate
        if flags & 0x0200:
            take(1)  # metabolic equivalent
        if flags & 0x0400:
            chunk = take(2)  # elapsed time, seconds
            elapsed = int.from_bytes(chunk, "little") if chunk else None
        if flags & 0x0800:
            take(2)  # remaining time
        if flags & 0x1000:
            take(2)  # energy per minute
        if flags & 0x2000:
            chunk = take(2)  # KingSmith extension: step count (u16)
            steps = int.from_bytes(chunk, "little") if chunk else None
        changes = {}
        if speed is not None:
            changes["speed"] = speed
        if distance is not None:
            changes["distance"] = distance
        if elapsed is not None:
            changes["elapsed"] = elapsed
        if steps is not None:
            changes["steps"] = steps
        if changes:
            self._set(**changes)

    # -- vendor channel ---------------------------------------------------

    async def vendor_request(self, frame, expect=None, timeout=3.0):
        # One vendor exchange in flight at a time; the reply arrives on the
        # supplement notify characteristic subscribed at connect time.
        async with self.vendor_lock:
            future = None
            if expect is not None:
                future = asyncio.get_running_loop().create_future()
                self.vendor_reply = future
            try:
                await self._paced()
                await self.client.write_gatt_char(KS_WRITE, frame, response=False)
                if future is None:
                    return None
                reply = await asyncio.wait_for(future, timeout)
                if reply[0:2] != bytes(expect):
                    raise RuntimeError(
                        "vendor reply %s != expected %s"
                        % (reply.hex(), bytes(expect).hex()))
                return reply
            finally:
                self.vendor_reply = None

    async def unlock(self, name):
        # The unlock write is fire-and-forget; success arrives as the 71 80
        # notification. The macOS client re-sends once at 5 s inside a 10 s
        # window, so this does the same.
        frame = unlock_frame(name)
        for attempt in range(2):
            try:
                await self.vendor_request(frame, expect=b"\x71\x80", timeout=10.0)
                return True
            except (asyncio.TimeoutError, RuntimeError) as exc:
                if attempt:
                    raise RuntimeError("vendor unlock rejected: %s" % exc) from exc
                await asyncio.sleep(5.0)
        return False

    async def read_properties(self):
        # SETTING_GET (all): 72 00 01 00 -> 72 80, data = 4-byte records
        # [id, error, lo, hi]; value is u16 LE, records with error != 0 are
        # skipped. Known ids: 1 units, 2 auto-stop, 4 motor version,
        # 5 last error, 6 child lock, 8 switches, 10 device mode.
        reply = await self.vendor_request(
            ks_frame(0x72, 0x00, [0x00]), expect=b"\x72\x80", timeout=3.0)
        records = []
        if reply is not None and len(reply) >= 4:
            data = reply[3:3 + reply[2]]
            for i in range(0, len(data) - 3, 4):
                prop_id, error, lo, hi = data[i:i + 4]
                if error == 0:
                    records.append({"id": prop_id, "value": lo | (hi << 8)})
        return records

    async def write_property(self, prop_id, value):
        # SETTING_SET: 72 01 03 <id> <lo> <hi> -> 72 81; data[1] == 0 means OK.
        frame = ks_frame(0x72, 0x01, [prop_id, value & 0xFF, (value >> 8) & 0xFF])
        reply = await self.vendor_request(frame, expect=b"\x72\x81", timeout=3.0)
        data = reply[3:3 + reply[2]] if reply is not None else b""
        return len(data) >= 2 and data[1] == 0

    # -- FTMS control point ----------------------------------------------

    async def _cp_once(self, op, data=b"", timeout=3.0):
        future = asyncio.get_running_loop().create_future()
        self.cp_reply[op] = future
        try:
            await self._paced()
            await self.client.write_gatt_char(
                FTMS_CP, bytes([op]) + bytes(data), response=True)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.cp_reply.pop(op, None)

    async def cp(self, op, data=b"", timeout=3.0):
        # Serialized control-point exchange with the documented recovery for
        # result 5 "control not permitted": re-claim control, retry once.
        async with self.cp_lock:
            result = await self._cp_once(op, data, timeout)
            if result == 5:
                await self._cp_once(OP_REQUEST_CONTROL, timeout=timeout)
                result = await self._cp_once(op, data, timeout)
            return result

    async def request_control(self):
        # REQUEST_CONTROL is required once before any command. Without the
        # vendor unlock the pad ignores it — the exact failure the library
        # CLI shows. Post-unlock it is granted within a few attempts.
        for _ in range(3):
            try:
                if await self.cp(OP_REQUEST_CONTROL) == 1:
                    self._set(control=True)
                    return True
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(1.0)
        return False

    # -- belt commands ----------------------------------------------------

    async def cmd_start(self, speed=None):
        # Start on a moving belt is refused (result 4) — if it is already
        # running, just retarget the speed.
        self.pause_pending = False
        if self.state["belt"] == "started":
            return await self.cmd_speed(speed) if speed else 1
        result = await self.cp(OP_START)
        if result == 1:
            # Accepted: the pad now counts down ~3 s before the belt moves.
            # Surface that window so clients don't look dead; the flag
            # clears on the machine-status "started" frame (or any belt
            # change) and, if that frame is missed, via the watchdog.
            self._set(starting=True)
            asyncio.get_running_loop().create_task(self._starting_watchdog())
            if speed:
                # The pad ramps to minimum speed first; retargeting too
                # early is unreliable, so wait for telemetry to report
                # speed > 0 plus a stabilization period, like the library
                # does.
                await self._wait(lambda: self.state["speed"] > 0, 5.0)
                await asyncio.sleep(1.0)
                result = await self.cmd_speed(speed)
        return result

    async def _starting_watchdog(self):
        await asyncio.sleep(8.0)
        if self.state.get("starting") and self.state["belt"] != "started":
            self._set(starting=False)

    async def cmd_speed(self, speed):
        # Belt refuses speed writes while stopped; clamp to the reported
        # range (fallback 1.0-6.0 km/h, observed on a KS-HD-Z1E).
        lo = self.state["min_speed"] or 1.0
        hi = self.state["max_speed"] or 6.0
        target = min(max(float(speed), lo), hi)
        return await self.cp(
            OP_SET_SPEED, int(round(target * 100)).to_bytes(2, "little"))

    async def cmd_stop(self):
        # Stop (08 01) finalizes the pad's session and resets its counters;
        # pause (08 02) keeps the session open.
        return await self.cp(OP_STOP_PAUSE, b"\x01")

    async def cmd_pause(self):
        # Pause keeps the pad's session open (counters keep accumulating on
        # resume); mark it so the eventual stop record isn't mistaken for
        # two sessions.
        self.pause_pending = True
        return await self.cp(OP_STOP_PAUSE, b"\x02")

    async def cmd_set_buzzer(self, enabled):
        # Property 8 "switches": bit 1 = buzzer, bit 3 = interaction light.
        # Documented on a Z1 (firmware V0.0.6); the bit polarity on Z1E is
        # unverified, but a read-modify-write touching only bit 1 is easily
        # reversible, and silencing the buzzer also silences the per-command
        # acknowledgement beeps.
        for record in await self.read_properties():
            if record["id"] == 8:
                value = record["value"]
                new_value = (value | 0x02) if enabled else (value & ~0x02)
                ok = await self.write_property(8, new_value)
                return ok, value, new_value
        return False, None, None

    # -- connection lifecycle -----------------------------------------------

    async def connect_once(self):
        device = await find_device(self.address, self.prefix)
        if device is None:
            # The pad does not advertise while a GATT connection exists,
            # so a failed scan means either the pad is off or something
            # still holds its link (a stale one bluetoothd never tore
            # down, or a phone-app session). The daemon owns the pad
            # (module policy), so drop it either way and let the next
            # retry connect; with nothing connected this is a no-op.
            if self.address and await bluez_force_disconnect(self.address):
                print("walkingpad: dropped stale BlueZ connection "
                      "to %s" % self.address, file=sys.stderr)
            raise RuntimeError("no WalkingPad found (is it powered on?)")
        name = device.name
        client = BleakClient(device)
        await client.connect()
        self._set(device={"address": device.address, "name": name})
        self.client = client
        self._set(connected=True)
        print("walkingpad: connected to %s (%s)" % (device.name, device.address),
              file=sys.stderr)
        # Subscribe BEFORE any vendor write — the unlock reply arrives on the
        # supplement notify characteristic.
        await client.start_notify(KS_NOTIFY, self.on_vendor)
        await client.start_notify(FTMS_CP, self.on_cp)
        await client.start_notify(FTMS_DATA, self.on_data)
        await client.start_notify(FTMS_STATUS, self.on_status)
        if not await self.unlock(device.name):
            raise RuntimeError("vendor unlock rejected (no 71 80 reply)")
        self._set(unlocked=True)
        print("walkingpad: vendor unlock acknowledged (71 80)", file=sys.stderr)
        # Best-effort static reads; either may be absent on some revisions.
        try:
            rng = bytes(await client.read_gatt_char(FTMS_SPEED_RANGE))
            if len(rng) >= 4:
                self._set(min_speed=int.from_bytes(rng[0:2], "little") / 100.0,
                          max_speed=int.from_bytes(rng[2:4], "little") / 100.0)
        except Exception:
            pass
        try:
            fw = bytes(await client.read_gatt_char(FIRMWARE_REV))
            self._set(firmware=fw.decode(errors="replace").strip("\x00 "))
        except Exception:
            pass
        if not await self.request_control():
            print("walkingpad: WARNING control not granted yet; commands may "
                  "be refused until it is", file=sys.stderr)
        print("walkingpad: pad ready", file=sys.stderr)
        # Keepalive: a silent property read every keepalive seconds surfaces
        # dead links quickly (the write raises once bluetoothd notices) and
        # keeps idle connections from being dropped.
        while True:
            if self.keepalive > 0:
                await asyncio.sleep(self.keepalive)
                await self.vendor_request(
                    ks_frame(0x72, 0x00, [0x00]), expect=b"\x72\x80", timeout=10.0)
            else:
                await asyncio.sleep(3600.0)

    async def run(self):
        while True:
            try:
                await self.connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("walkingpad: %s; reconnecting in %.0fs"
                      % (exc, self.reconnect_delay), file=sys.stderr)
                if self.client is not None:
                    try:
                        await self.client.disconnect()
                    except Exception as exc:
                        print("walkingpad: disconnect after failure "
                              "errored: %s" % exc, file=sys.stderr)
                    self.client = None
                    # Any connection bluetoothd still holds to the pad at
                    # this point is this daemon's own leak, and the pad
                    # will not advertise while it exists — drop it now so
                    # the next scan can find the pad.
                    if self.address and await bluez_force_disconnect(
                            self.address):
                        print("walkingpad: dropped leftover BlueZ "
                              "connection to %s"
                              % self.address, file=sys.stderr)
                self._set(connected=False, unlocked=False, control=False,
                          belt="unknown")
                await asyncio.sleep(self.reconnect_delay)


async def dispatch(pad, request):
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    if cmd == "history":
        # History is served over the socket: the state directory belongs
        # to the service's dynamic user and is not reliably readable by
        # other users, so clients must not parse the files directly.
        return {"ok": True, "history": pad.read_history(),
                "current": pad._read_current()}
    if cmd == "watch":
        return {"ok": True, "state": pad.state}
    if not pad.state["connected"]:
        return {"ok": False, "error": "pad not connected", "state": pad.state}
    if cmd == "status":
        return {"ok": True, "state": pad.state}
    if cmd == "properties":
        return {"ok": True, "properties": await pad.read_properties()}
    if cmd == "start":
        result = await pad.cmd_start(request.get("speed"))
    elif cmd == "speed":
        try:
            result = await pad.cmd_speed(request["speed"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "speed must be a number"}
    elif cmd == "stop":
        result = await pad.cmd_stop()
    elif cmd == "pause":
        result = await pad.cmd_pause()
    elif cmd == "set-buzzer":
        enabled = request.get("value") in (True, 1, "on", "true")
        ok, old, new = await pad.cmd_set_buzzer(enabled)
        return {"ok": ok, "old": old, "new": new}
    else:
        return {"ok": False, "error": "unknown cmd: %r" % (cmd,)}
    return {"ok": result == 1, "result": result,
            "result_text": RESULT_TEXT.get(result, "unknown"),
            "state": pad.state}


async def handle_client(reader, writer, pad):
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                request = json.loads(line)
            except ValueError:
                writer.write((json.dumps(
                    {"ok": False, "error": "invalid JSON"}) + "\n").encode())
                await writer.drain()
                continue
            reply = await dispatch(pad, request)
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            if request.get("cmd") == "watch":
                # Stream state updates until the client goes away.
                queue = asyncio.Queue()
                pad.watchers.add(queue)
                queue.put_nowait(dict(pad.state))
                try:
                    while True:
                        state = await queue.get()
                        writer.write((json.dumps(
                            {"ok": True, "state": state}) + "\n").encode())
                        await writer.drain()
                except (ConnectionError, OSError):
                    return
                finally:
                    pad.watchers.discard(queue)
    except (ConnectionError, OSError):
        return


def parse_args():
    parser = argparse.ArgumentParser(
        prog="walkingpad-daemon",
        description="Persistent control daemon for KingSmith WalkingPad "
                    "Z1-series belts")
    parser.add_argument("--socket", default="/run/walkingpad/daemon.sock",
                        help="path of the JSON-lines control socket")
    parser.add_argument("--address", default=None,
                        help="BLE address of the pad (skips name matching)")
    parser.add_argument("--prefix", default="KS-",
                        help="BLE name prefix to match during discovery")
    parser.add_argument("--keepalive", type=float, default=30.0,
                        help="seconds between silent link checks (0 disables)")
    parser.add_argument("--reconnect-delay", type=float, default=5.0,
                        help="seconds to wait before reconnecting")
    parser.add_argument("--history", default="/var/lib/walkingpad/history.jsonl",
                        help="JSON-lines session history (one record per "
                             "finalized session); empty string disables")
    return parser.parse_args()


async def amain():
    args = parse_args()
    pad = Pad(args)
    socket_dir = os.path.dirname(args.socket)
    if socket_dir:
        os.makedirs(socket_dir, exist_ok=True)
    if os.path.exists(args.socket):
        os.unlink(args.socket)  # stale socket from a previous run
    server = await asyncio.start_unix_server(
        lambda reader, writer: handle_client(reader, writer, pad),
        path=args.socket)
    os.chmod(args.socket, 0o666)  # any local user may steer the belt
    asyncio.create_task(pad.run())
    async with server:
        await server.serve_forever()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
