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
     stop/pause (parameter 01/02), 01 reset, 03 safety stop, FF control lost)

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

RESULT_TEXT = {
    1: "success",
    2: "op not supported",
    3: "invalid parameter",
    4: "failed",
    5: "control not permitted",
}

WRITE_PACE = 0.4  # documented spacing within each command channel


class ReconnectRequested(Exception):
    """Raised to drop the current link and reconnect without delay."""


def _queue_start(pad, request):
    # A start pressed while the pad is unreachable (standby dropped the
    # link, or bluetoothd holds a stale one) should not be lost: queue
    # it, flag the state stream so clients show "starting…" through the
    # reconnect window, and wake the reconnect loop immediately.
    pad.pending_start = {"speed": request.get("speed")}
    pad.mark_starting()
    pad.reconnect_now.set()


FLUSH_INTERVAL = 5.0  # min seconds between current.json rewrites

# Assume continuation unless a modulo delta exceeds these thresholds.
# This suits short reconnects/reboots, but cannot identify every missed
# session boundary. See README.md for the deliberate tradeoffs.
PAD_TO_RECORD = {
    "distance": ("distance", 0xFFFFFF),  # u24 meters
    "elapsed": ("duration", 0xFFFF),      # u16 seconds of walking
    "steps": ("steps", 0xFFFF),           # u16 steps
}
RESET_THRESHOLD = {"distance": 50000, "duration": 3600, "steps": 50000}


def parse_telemetry(data):
    """Decode a complete notification; discard truncated frames atomically."""
    # Fields in flag-bit order. None means consume without exposing it.
    fields = (
        (2, "speed"),     # bit 0 clear: instantaneous speed (km/h * 100)
        (2, None),        # average speed
        (3, "distance"),  # metres
        (4, None),        # inclination + ramp angle
        (4, None),        # positive + negative elevation gain
        (1, None),        # instantaneous pace
        (1, None),        # average pace
        (5, None),        # total energy + energy/hour + energy/minute
        (1, None),        # heart rate
        (1, None),        # metabolic equivalent
        (2, "elapsed"),   # seconds
        (2, None),        # remaining time
        (4, None),        # force on belt + power output (s16 x2)
        (2, "steps"),     # KingSmith extension (u16)
    )
    if len(data) < 2:
        return {}
    flags = int.from_bytes(data[:2], "little")
    offset = 2
    values = {}
    for bit, (size, name) in enumerate(fields):
        present = flags & (1 << bit)
        if bit == 0:
            present = not present
        if not present:
            continue
        end = offset + size
        if end > len(data):
            return {}
        if name is not None:
            values[name] = int.from_bytes(data[offset:end], "little")
        offset = end
    if "speed" in values:
        values["speed"] /= 100.0
    return values


class Sessions:
    """Session deltas and optional persistence; raw counters stay internal."""

    def __init__(self, path):
        self.path = path
        self.directory = os.path.dirname(path) or "."
        self.current_path = os.path.join(self.directory, "current.json")
        self.failed = False
        self.last_flush = 0.0
        self.session = None
        self.previous = {}
        self.stopped = False
        self.revision = 0
        if path:
            try:
                with open(self.current_path, encoding="utf-8") as fh:
                    snapshot = json.load(fh)
                session, previous = snapshot["session"], snapshot["prev"]
                if not isinstance(session, dict) or not isinstance(previous, dict):
                    return
                if any(type(session.get(field)) is not int or session[field] < 0
                       for field in RESET_THRESHOLD):
                    return
                if any(type(value) is not int or not 0 <= value <= PAD_TO_RECORD[field][1]
                       for field, value in previous.items()):
                    return
                self.session = {field: session[field] for field in RESET_THRESHOLD}
                self.previous = previous
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def start(self):
        # A start following a known stop establishes a new zero baseline.
        # Resuming a pause or reconnecting retains the active session.
        if self.session is None:
            self.previous = {}
        self.stopped = False

    def update(self, counters, walking):
        reset = any(
            ((counters[field] - self.previous[field]) & mask) > RESET_THRESHOLD[record]
            for field, (record, mask) in PAD_TO_RECORD.items()
            if field in counters and field in self.previous)
        if reset:
            self.reset()
        # Ignore telemetry trailing a known stop until a reset or start.
        if self.stopped:
            return
        if self.session is None:
            if not walking:
                return
            self.session = dict.fromkeys(RESET_THRESHOLD, 0)
        for field, (record, mask) in PAD_TO_RECORD.items():
            if field in counters:
                old = self.previous.get(field, 0)
                self.session[record] += (counters[field] - old) & mask
                self.previous[field] = counters[field]
        self.flush()

    def reset(self):
        self.finish()
        self.previous = {}
        self.stopped = False

    def live(self):
        totals = self.session or {}
        return {field: totals.get(record, 0)
                for field, (record, _mask) in PAD_TO_RECORD.items()}

    def current(self):
        if self.session is None:
            return None
        return {"ts": int(time.time()), "date": time.strftime("%Y-%m-%d"),
                "session": dict(self.session), "prev": dict(self.previous)}

    def finish(self):
        self.stopped = True
        session, self.session = self.session, None
        self.last_flush = 0.0
        if session is None:
            return
        if any(session.values()):
            self.revision += 1
        if not self.path or self.failed:
            return
        try:
            if any(session.values()):
                os.makedirs(self.directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    record = {"ts": int(time.time()), "date": time.strftime("%Y-%m-%d"),
                              "reason": "stop", **session}
                    fh.write(json.dumps(record) + "\n")
            # Remove even a zero-session snapshot, so it cannot be recovered.
            if os.path.exists(self.current_path):
                os.unlink(self.current_path)
        except OSError as exc:
            self._disable(exc)

    def read(self):
        records = []
        if self.path:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            record = json.loads(line)
                            if isinstance(record, dict):
                                records.append(record)
                        except ValueError:
                            continue
            except OSError:
                pass
        return records

    def flush(self):
        if not self.path or self.failed or self.session is None:
            return
        now = time.monotonic()
        if now - self.last_flush < FLUSH_INTERVAL:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = self.current_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.current()) + "\n")
            os.replace(tmp, self.current_path)
            self.last_flush = now
        except OSError as exc:
            self._disable(exc)

    def _disable(self, exc):
        self.failed = True
        print("walkingpad: history disabled (%s)" % exc, file=sys.stderr)


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


def _dbus_value(variant, default=None):
    # dbus-fast returns properties from GetManagedObjects as Variant
    # wrappers; comparing a Variant against a plain value never matches.
    if variant is None:
        return default
    return getattr(variant, "value", variant)


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
        from dbus_fast.auth import AuthExternal, UID_NOT_SPECIFIED
    except ImportError:
        return False
    bus = await MessageBus(
        bus_type=BusType.SYSTEM,
        auth=AuthExternal(uid=UID_NOT_SPECIFIED)).connect()
    try:
        reply = await bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects"))
        objects = reply.body[0] if reply.body else {}
        for path, interfaces in objects.items():
            props = (interfaces or {}).get("org.bluez.Device1")
            if not props:
                continue
            if _dbus_value(props.get("Address")) != address:
                continue
            if not _dbus_value(props.get("Connected"), False):
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
        self.sessions = Sessions(args.history)
        # Immediate-reconnect request (a start pressed while the pad is
        # in standby must not wait for the keepalive to notice the dead
        # link), and a start queued across that reconnect.
        self.reconnect_now = asyncio.Event()
        self.pending_start = None
        self.starting_task = None
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
            "history_revision": self.sessions.revision,
            **self.sessions.live(),
            "min_speed": None,
            "max_speed": None,
            "firmware": None,
            "device": None,
        }

    def _set(self, **changes):
        self.state.update(changes)
        self.state["history_revision"] = self.sessions.revision
        for queue in self.watchers:
            queue.put_nowait(dict(self.state))

    async def _paced(self):
        # Each channel serializes its exchanges; cross-channel spacing is
        # not known to be required by the firmware.
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
        op = frame[0]
        if op == 0x04:
            self.sessions.start()
            self._set(belt="started", starting=False)
        elif op in (0x01, 0x03) or frame[:2] == b"\x02\x01":
            if op == 0x01:
                self.sessions.reset()
            else:
                self.sessions.finish()
            self._set(belt="stopped", speed=0.0, starting=False,
                      **self.sessions.live())
        elif frame[:2] == b"\x02\x02":
            self._set(belt="paused", speed=0.0, starting=False)
        elif op == 0x02:
            # A missing/unknown parameter cannot distinguish stop from pause.
            # Preserve accounting until telemetry demonstrates a reset.
            self._set(belt="stopped", speed=0.0, starting=False)
        elif op == 0xFF:
            self._set(control=False)

    def on_data(self, _char, data):
        changes = parse_telemetry(data)
        if changes:
            walking = changes.get("speed", self.state["speed"]) > 0
            self.sessions.update(changes, walking)
            if walking and self.sessions.session is not None:
                changes.update(belt="started", starting=False)
            changes.update(self.sessions.live())
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
        if self.state["belt"] == "started":
            return await self.cmd_speed(speed) if speed else 1
        result = await self.cp(OP_START)
        if result == 1:
            self.sessions.start()
            # Accepted: the pad now counts down ~3 s before the belt moves.
            # Surface that window so clients don't look dead; the flag
            # clears on the machine-status "started" frame (or any belt
            # change) and, if that frame is missed, via the watchdog.
            if self.state["belt"] != "started":
                self.mark_starting()
            if speed:
                # The pad ramps to minimum speed first; retargeting too
                # early is unreliable, so wait for telemetry to report
                # speed > 0 plus a stabilization period, like the library
                # does.
                await self._wait(lambda: self.state["speed"] > 0, 5.0)
                await asyncio.sleep(1.0)
                result = await self.cmd_speed(speed)
        return result

    async def _idle_wait(self, timeout):
        # Returns True when a reconnect was requested (the caller drops
        # the link and reconnects without delay), False on timeout. The
        # event is consumed either way.
        try:
            await asyncio.wait_for(self.reconnect_now.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        self.reconnect_now.clear()
        return True

    def _run_queued_start(self):
        # Runs after "pad ready": a start that was queued while the pad
        # was unreachable finally has a live link.
        if self.pending_start is None:
            return
        queued = self.pending_start
        self.pending_start = None

        async def run():
            try:
                result = await self.cmd_start(queued["speed"])
                if result != 1:
                    print("walkingpad: queued start refused (result %s)"
                          % result, file=sys.stderr)
            except Exception as exc:
                print("walkingpad: queued start failed: %s" % exc,
                      file=sys.stderr)

        asyncio.get_running_loop().create_task(run())

    async def _starting_watchdog(self):
        # 30 s covers a full reconnect (scan timeout 10 s + retry) plus
        # the pad's ~3 s countdown. New starts replace this watchdog.
        await asyncio.sleep(30.0)
        if self.state.get("starting") and self.state["belt"] != "started":
            self._set(starting=False)

    def mark_starting(self):
        if self.starting_task is not None:
            self.starting_task.cancel()
        self._set(starting=True)
        self.starting_task = asyncio.create_task(self._starting_watchdog())

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
        result = await self.cp(OP_STOP_PAUSE, b"\x01")
        if result == 1:
            # Also handles firmware that omits the status notification.
            self.sessions.finish()
            self._set(belt="stopped", speed=0.0, starting=False,
                      **self.sessions.live())
        return result

    async def cmd_pause(self):
        result = await self.cp(OP_STOP_PAUSE, b"\x02")
        if result == 1:
            self._set(belt="paused", speed=0.0, starting=False)
        return result

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
        self._run_queued_start()
        # Keepalive: a silent property read every keepalive seconds surfaces
        # dead links quickly (the write raises once bluetoothd notices) and
        # keeps idle connections from being dropped. A reconnect request
        # interrupts the idle wait and drops the link immediately.
        while True:
            if self.keepalive > 0:
                if await self._idle_wait(self.keepalive):
                    raise ReconnectRequested()
                await self.vendor_request(
                    ks_frame(0x72, 0x00, [0x00]), expect=b"\x72\x80", timeout=10.0)
            else:
                if await self._idle_wait(3600.0):
                    raise ReconnectRequested()

    async def run(self):
        last_error = None
        while True:
            try:
                await self.connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                immediate = isinstance(exc, ReconnectRequested)
                error = (type(exc), str(exc))
                # An unavailable pad is normal between uses. Report changes,
                # and fresh failures after connecting, without logging every retry.
                if immediate or self.state["connected"] or error != last_error:
                    print("walkingpad: %s; reconnecting %s"
                          % (exc, "immediately" if immediate
                             else "in %.0fs" % self.reconnect_delay),
                          file=sys.stderr)
                last_error = None if immediate else error
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
                if not immediate:
                    await self._idle_wait(self.reconnect_delay)


async def dispatch(pad, request):
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    if cmd == "history":
        # History is served over the socket: the state directory belongs
        # to the service's dynamic user and is not reliably readable by
        # other users, so clients must not parse the files directly.
        return {"ok": True, "history": pad.sessions.read(),
                "current": pad.sessions.current(),
                "history_revision": pad.sessions.revision}
    if cmd == "watch":
        return {"ok": True, "state": pad.state}
    if cmd == "reconnect":
        # Drop the current link (if any) and reconnect without delay —
        # e.g. when the pad went into standby and the client knows it
        # needs a fresh link now rather than at the next keepalive.
        pad.reconnect_now.set()
        return {"ok": True, "state": pad.state}
    if not pad.state["connected"]:
        if cmd == "stop" and pad.pending_start is not None:
            pad.pending_start = None
            pad._set(starting=False)
            return {"ok": True, "state": pad.state}
        if cmd == "start":
            # Queue the start across the reconnect; the state stream
            # reports starting=true throughout the reconnect window.
            _queue_start(pad, request)
            return {"ok": True, "queued": "start", "state": pad.state}
        return {"ok": False, "error": "pad not connected", "state": pad.state}
    if cmd == "status":
        return {"ok": True, "state": pad.state}
    if cmd == "properties":
        return {"ok": True, "properties": await pad.read_properties()}
    if cmd == "start":
        try:
            result = await pad.cmd_start(request.get("speed"))
        except Exception:
            # The link died mid-command (the pad's standby can drop it
            # silently): re-queue the start and reconnect now instead
            # of losing the press.
            _queue_start(pad, request)
            return {"ok": True, "queued": "start", "state": pad.state}
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
