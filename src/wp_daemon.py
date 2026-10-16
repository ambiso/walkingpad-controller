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
from contextvars import ContextVar
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

WRITE_PACE = 0.4  # minimum spacing between BLE exchanges
START_TIMEOUT = 8.0  # from the request until movement is confirmed


class PropertyWriteError(RuntimeError):
    """The pad answered a property write without confirming success."""


class ReconnectRequested(Exception):
    """Raised to drop the current link and reconnect without delay."""


class Superseded(RuntimeError):
    """A newer movement request cancelled this work."""


class ExchangeQueue:
    """Serialize short BLE exchanges; Stop precedes ordinary queued work."""

    def __init__(self, pace):
        self.pace = pace
        self.queue = asyncio.PriorityQueue()
        self.sequence = 0
        self.worker = None

    async def submit(self, operation, priority):
        future = asyncio.get_running_loop().create_future()
        self.sequence += 1
        self.queue.put_nowait((priority, self.sequence, operation, future))
        if self.worker is None:
            self.worker = asyncio.create_task(self.run())
        return await future

    async def run(self):
        try:
            while not self.queue.empty():
                # Choose after pacing, so Stop can overtake work that has
                # not yet been sent. An exchange on the wire is drained.
                await self.pace()
                _priority, _sequence, operation, future = self.queue.get_nowait()
                if future.cancelled():
                    continue
                try:
                    result = await operation()
                except asyncio.CancelledError:
                    future.cancel()
                    raise
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
        finally:
            self.worker = None


def _queue_start(pad, request, epoch=None):
    epoch = pad.begin_start() if epoch is None else epoch
    if epoch != pad.motion_epoch:
        raise Superseded("start cancelled")
    pad.pending_start = {"speed": request.get("speed"), "epoch": epoch}
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
    # Pin discovery by address if given, otherwise match
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
        self.exchanges = ExchangeQueue(lambda: self._paced())
        # Each movement sequence carries a guard through its nested BLE
        # calls. The queue captures it; worker-task context is never relied on.
        self.intent = ContextVar("walkingpad_intent", default=None)
        self.motion_epoch = 0
        self.speed_revision = 0
        self.link_epoch = 0
        self.link_broken = False
        self.pending_stop = False
        self.vendor_reply = None
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
        if self.vendor_reply is not None:
            expected, future = self.vendor_reply
            if frame[:2] == expected and not future.done():
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
        # A stop/pause report can belong to an earlier command. It updates
        # observed state, but must not cancel a newer requested Start.
        op = frame[0]
        if op == 0x04:
            self.sessions.start()
            self._set(belt="started", starting=False)
        elif op in (0x01, 0x03) or frame[:2] == b"\x02\x01":
            if op == 0x01:
                self.sessions.reset()
            else:
                self.sessions.finish()
            self._set(belt="stopped", speed=0.0,
                      **self.sessions.live())
        elif frame[:2] == b"\x02\x02":
            self._set(belt="paused", speed=0.0)
        elif op == 0x02:
            # A missing/unknown parameter cannot distinguish stop from pause.
            # Preserve accounting until telemetry demonstrates a reset.
            self._set(belt="stopped", speed=0.0)
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

    def check_intent(self):
        guard = self.intent.get()
        if guard is not None and not guard():
            raise Superseded("movement request superseded")

    def break_link(self, wake=True):
        # Replies have no transaction IDs. After a timeout, no new exchange
        # may reuse this connection and mistake a late reply for its own.
        self.link_epoch += 1
        self.link_broken = True
        self._set(connected=False, unlocked=False, control=False, belt="unknown")
        pending = list(self.cp_reply.values())
        if self.vendor_reply is not None:
            pending.append(self.vendor_reply[1])
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError("Bluetooth connection changed"))
        if wake:
            self.reconnect_now.set()

    async def exchange(self, characteristic, frame, expected, timeout, priority=10):
        client, generation = self.client, self.link_epoch
        guard = self.intent.get()

        def check():
            if guard is not None and not guard():
                raise Superseded("movement request superseded")
            if self.link_broken or self.client is not client or self.link_epoch != generation:
                raise ConnectionError("Bluetooth connection changed")

        async def perform():
            check()
            future = asyncio.get_running_loop().create_future()
            if characteristic == FTMS_CP:
                self.cp_reply[expected] = future
            else:
                self.vendor_reply = (bytes(expected), future)
            try:
                async with asyncio.timeout(timeout):
                    self.last_write = time.monotonic()
                    await client.write_gatt_char(
                        characteristic, frame, response=characteristic == FTMS_CP)
                    return await future
            except Exception:
                if self.client is client and self.link_epoch == generation:
                    self.break_link()
                raise
            finally:
                if characteristic == FTMS_CP:
                    self.cp_reply.pop(expected, None)
                else:
                    self.vendor_reply = None

        return await self.exchanges.submit(perform, priority)

    async def vendor_request(self, frame, expect, timeout=3.0):
        return await self.exchange(KS_WRITE, frame, expect, timeout)

    async def unlock(self, name):
        # A missing reply requires a fresh connection, not an ambiguous retry.
        await self.vendor_request(unlock_frame(name), expect=b"\x71\x80", timeout=10.0)
        return True

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
        # SETTING_SET: 72 01 03 <id> <lo> <hi> -> 72 81 <len> <id> <error>.
        frame = ks_frame(0x72, 0x01, [prop_id, value & 0xFF, (value >> 8) & 0xFF])
        reply = await self.vendor_request(frame, expect=b"\x72\x81", timeout=3.0)
        data = reply[3:3 + reply[2]] if reply is not None else b""
        if len(data) != 2 or data[0] != prop_id:
            raise PropertyWriteError("unexpected property %d write reply: %s"
                                     % (prop_id, reply.hex() if reply is not None else "none"))
        if data[1]:
            raise PropertyWriteError("property %d write rejected: error %d, reply %s"
                                     % (prop_id, data[1], reply.hex()))
        return True

    # -- FTMS control point ----------------------------------------------

    async def _cp_once(self, op, data=b"", timeout=3.0, priority=10):
        return await self.exchange(FTMS_CP, bytes([op]) + bytes(data), op, timeout, priority)

    async def cp(self, op, data=b"", timeout=3.0, priority=10):
        result = await self._cp_once(op, data, timeout, priority)
        if result == 5:
            await self._cp_once(OP_REQUEST_CONTROL, timeout=timeout, priority=priority)
            result = await self._cp_once(op, data, timeout, priority)
        return result

    async def request_control(self):
        # REQUEST_CONTROL is required once before any command. Without the
        # vendor unlock the pad ignores it — the exact failure the library
        # CLI shows. Post-unlock it is granted within a few attempts.
        for _ in range(3):
            if await self.cp(OP_REQUEST_CONTROL) == 1:
                self._set(control=True)
                return True
            await asyncio.sleep(1.0)
        return False

    # -- belt commands ----------------------------------------------------

    async def wake_for_start(self):
        # Property 10, bits 5–7: manual=0, auto=1, sleep=2.
        # Z1E J41_V301.10.22 refuses FTMS Start while asleep (result 4).
        # Remote wake changed 0x0240 to 0x0200 on that hardware; preserve
        # every bit outside the mode field when requesting manual mode.
        # https://github.com/slandau3/z1-walkingpad-mcp/blob/main/docs/protocol.md#properties-observed-on-the-z1
        for record in await self.read_properties():
            if record["id"] == 10:
                value = record["value"]
                if ((value >> 5) & 7) != 2:
                    return True
                if not await self.write_property(10, value & ~0xE0):
                    return False
                # Check that waking succeeded before attempting to move.
                return any(
                    item["id"] == 10 and ((item["value"] >> 5) & 7) == 0
                    for item in await self.read_properties())
        return True

    def cancel_pending_motion(self):
        self.motion_epoch += 1
        self.speed_revision += 1
        self.pending_start = None
        if self.starting_task is not None:
            self.starting_task.cancel()
        self._set(starting=False)

    def begin_start(self):
        self.cancel_pending_motion()
        if self.state["belt"] != "started":
            self.mark_starting()
        return self.motion_epoch

    async def cmd_start(self, speed=None, epoch=None):
        epoch = self.begin_start() if epoch is None else epoch
        generation, speed_revision = self.link_epoch, self.speed_revision
        token = self.intent.set(lambda: self.motion_epoch == epoch and self.link_epoch == generation)
        try:
            self.check_intent()
            if self.state["belt"] == "started":
                return await self.cmd_speed(speed) if speed is not None else 1
            if not await self.wake_for_start():
                return 4
            self.check_intent()
            result = await self.cp(OP_START)
            self.check_intent()
            if result == 1:
                self.sessions.start()
                if speed is not None:
                    moving = await self._wait(lambda: self.state["speed"] > 0, 5.0)
                    self.check_intent()
                    if moving:
                        await asyncio.sleep(1.0)
                        self.check_intent()
                        if self.speed_revision == speed_revision:
                            result = await self.cmd_speed(speed)
            return result
        finally:
            self.intent.reset(token)

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

    def _resume_commands(self):
        if not self.pending_stop and self.pending_start is None:
            return

        async def resume():
            try:
                if self.pending_stop and await self._stop() != 1:
                    return
                queued = self.pending_start
                self.pending_start = None
                if queued is not None:
                    result = await self.cmd_start(queued["speed"], queued["epoch"])
                    if result != 1 and queued["epoch"] == self.motion_epoch:
                        self.cancel_pending_motion()
            except Superseded:
                pass
            except Exception as exc:
                print("walkingpad: queued command failed: %s" % exc, file=sys.stderr)

        asyncio.create_task(resume())

    async def _starting_watchdog(self):
        await asyncio.sleep(START_TIMEOUT)
        if self.state["starting"]:
            self.starting_task = None
            self.cancel_pending_motion()
            # Start may already have been accepted. Cancel its countdown
            # too, rather than only hiding an unconfirmed operation.
            if self.state["connected"]:
                self.pending_stop = True
                token = self.intent.set(None)
                try:
                    await self._stop()
                except Exception as exc:
                    print("walkingpad: stop after start timeout failed: %s" % exc, file=sys.stderr)
                finally:
                    self.intent.reset(token)

    def mark_starting(self):
        if self.starting_task is not None:
            self.starting_task.cancel()
        self._set(starting=True)
        self.starting_task = asyncio.create_task(self._starting_watchdog())

    async def cmd_speed(self, speed):
        lo = self.state["min_speed"] or 1.0
        hi = self.state["max_speed"] or 6.0
        target = min(max(float(speed), lo), hi)
        self.speed_revision += 1
        epoch, revision = self.motion_epoch, self.speed_revision
        generation = self.link_epoch
        token = self.intent.set(lambda: (epoch, revision, generation) == (
            self.motion_epoch, self.speed_revision, self.link_epoch))
        try:
            return await self.cp(OP_SET_SPEED, int(round(target * 100)).to_bytes(2, "little"))
        finally:
            self.intent.reset(token)

    async def _stop(self):
        epoch = self.motion_epoch
        result = await self.cp(OP_STOP_PAUSE, b"\x01", priority=0)
        if result == 1:
            self.pending_stop = False
            self.sessions.finish()
            newer_start = self.pending_start is not None or epoch != self.motion_epoch
            self._set(belt="stopped", speed=0.0,
                      starting=self.state["starting"] if newer_start else False,
                      **self.sessions.live())
        return result

    async def cmd_stop(self):
        self.cancel_pending_motion()
        self.pending_stop = True
        return await self._stop()

    async def cmd_pause(self):
        self.cancel_pending_motion()
        epoch, generation = self.motion_epoch, self.link_epoch
        token = self.intent.set(lambda: (epoch, generation) == (
            self.motion_epoch, self.link_epoch))
        try:
            result = await self.cp(OP_STOP_PAUSE, b"\x02", priority=1)
            if result == 1 and epoch == self.motion_epoch:
                self._set(belt="paused", speed=0.0, starting=False)
            return result
        finally:
            self.intent.reset(token)

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

    async def read_auto_stop(self):
        # KS Fit 6.6.0: property 2 is no-load stop, not idle sleep.
        for record in await self.read_properties():
            if record["id"] == 2:
                value = record["value"]
                return {"enabled": bool(value & 0x8000),
                        "supports_seconds": bool(value & 0x4000),
                        "custom_seconds": bool(value & 0x2000),
                        "seconds": value & 0x1FFF, "raw": value}
        raise RuntimeError("auto-stop property unavailable")

    async def auto_stop(self, value=None, seconds=None):
        if value not in (None, "on", "off"):
            raise ValueError("value must be on or off")
        if seconds is not None:
            valid = value == "on" and type(seconds) is int and 1 <= seconds <= 8191
            if not valid:
                raise ValueError("seconds requires on and an integer from 1 to 8191")
        state = await self.read_auto_stop()
        if value is not None:
            enabled = value == "on"
            target = state["raw"] | 0x8000 if enabled else state["raw"] & ~0x8000
            mask = 0x8000
            if seconds is not None:
                if not state["supports_seconds"]:
                    raise ValueError("pad does not support setting auto-stop duration")
                target = (target & ~0x1FFF) | 0x2000 | seconds
                mask |= 0x3FFF
            if target != state["raw"]:
                if not await self.write_property(2, target):
                    raise RuntimeError("auto-stop write rejected")
                state = await self.read_auto_stop()
                if (state["raw"] & mask) != (target & mask):
                    return {"ok": False, "error": "pad did not confirm auto-stop setting",
                            "auto_stop": state}
        return {"ok": True, "auto_stop": state}

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
        self.link_epoch += 1
        self.link_broken = False
        generation = self.link_epoch
        self._set(connected=True)
        print("walkingpad: connected to %s (%s)" % (device.name, device.address),
              file=sys.stderr)
        # Subscribe BEFORE any vendor write — the unlock reply arrives on the
        # supplement notify characteristic.

        def current(handler):
            def notify(char, data):
                if self.client is client and self.link_epoch == generation:
                    handler(char, data)
            return notify

        await client.start_notify(KS_NOTIFY, current(self.on_vendor))
        await client.start_notify(FTMS_CP, current(self.on_cp))
        await client.start_notify(FTMS_DATA, current(self.on_data))
        await client.start_notify(FTMS_STATUS, current(self.on_status))
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
        if self.pending_start is not None:
            # A Start queued during discovery requested this connection;
            # its wake-up event must not immediately tear it down again.
            self.reconnect_now.clear()
        self._resume_commands()
        # Keepalive: a silent property read every keepalive seconds surfaces
        # dead links quickly (the write raises once bluetoothd notices) and
        # keeps idle connections from being dropped. A reconnect request
        # interrupts the idle wait and drops the link immediately.
        while True:
            if self.keepalive > 0:
                if await self._idle_wait(self.keepalive):
                    raise ReconnectRequested()
                await self.vendor_request(
                    ks_frame(0x72, 0x00, [0x00]), expect=b"\x72\x80")
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
                    self.break_link(wake=False)
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
        pad.cancel_pending_motion()
        pad.break_link()
        return {"ok": True, "state": pad.state}
    if not pad.state["connected"]:
        if cmd == "stop":
            pad.cancel_pending_motion()
            pad.pending_stop = True
            pad.reconnect_now.set()
            return {"ok": True, "queued": "stop", "state": pad.state}
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
    if cmd == "auto-stop":
        try:
            return await pad.auto_stop(request.get("value"), request.get("seconds"))
        except (ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            return {"ok": False, "error": str(exc) or "auto-stop request timed out"}
    if cmd == "start":
        epoch = pad.begin_start()
        try:
            result = await pad.cmd_start(request.get("speed"), epoch)
            if result != 1 and epoch == pad.motion_epoch:
                pad.cancel_pending_motion()
        except (PropertyWriteError, Superseded) as exc:
            if epoch == pad.motion_epoch:
                pad.cancel_pending_motion()
            return {"ok": False, "error": str(exc)}
        except Exception:
            # The link died mid-command (the pad's standby can drop it
            # silently): re-queue the start and reconnect now instead
            # of losing the press.
            if epoch != pad.motion_epoch:
                return {"ok": False, "error": "start cancelled"}
            _queue_start(pad, request, epoch)
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
            try:
                reply = await dispatch(pad, request)
            except Exception as exc:
                reply = {"ok": False, "error": str(exc) or type(exc).__name__}
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
