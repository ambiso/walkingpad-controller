"""Run with: python -m unittest discover -s tests."""

import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


spec = importlib.util.spec_from_file_location(
    "wp_daemon", Path(__file__).resolve().parents[1] / "src/wp_daemon.py")
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


def counters(duration=100, steps=150, distance=80):
    return {"elapsed": duration, "steps": steps, "distance": distance}


def telemetry(pad, duration=100, steps=150, distance=80, speed=300):
    frame = (b"\x04\x24" + speed.to_bytes(2, "little")
             + distance.to_bytes(3, "little")
             + duration.to_bytes(2, "little") + steps.to_bytes(2, "little"))
    pad.on_data(None, frame)


def make_pad(path=""):
    return daemon.Pad(argparse.Namespace(
        address=None, prefix="KS-", keepalive=30, reconnect_delay=5,
        history=path))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "history.jsonl")
        self.sessions = daemon.Sessions(self.path)

    def test_first_sample_includes_existing_activity(self):
        self.sessions.update(counters(), walking=True)
        self.assertEqual(self.sessions.live(), counters())
        self.sessions.finish()
        self.assertEqual(self.sessions.read()[0]["duration"], 100)

    def test_u16_wraps_and_live_totals(self):
        self.sessions.update(counters(65535, 65535, 100), walking=True)
        self.sessions.update(counters(0, 0, 101), walking=True)
        self.assertEqual(self.sessions.live(), counters(65536, 65536, 101))

    def test_u24_distance_wrap(self):
        self.sessions.update(counters(distance=0xFFFFFF), walking=True)
        self.sessions.update(counters(101, 152, 0), walking=True)
        self.assertEqual(self.sessions.live()["distance"], 0x1000000)

    def test_long_continuous_session(self):
        for seconds in range(0, 70001, 1000):
            self.sessions.update(counters(
                seconds & 0xFFFF, (seconds * 2) & 0xFFFF, seconds), True)
        self.sessions.finish()
        record = self.sessions.read()[0]
        self.assertEqual((record["duration"], record["steps"], record["distance"]),
                         (70000, 140000, 70000))

    def test_reset_seeds_new_session_from_first_frame(self):
        self.sessions.update(counters(), True)
        self.sessions.update(counters(10, 15, 8), True)
        self.assertEqual(self.sessions.read()[0]["duration"], 100)
        self.assertEqual(self.sessions.live(), counters(10, 15, 8))

    def test_any_counter_can_detect_reset(self):
        for field in counters():
            with self.subTest(field=field):
                sessions = daemon.Sessions("")
                sessions.update(counters(), True)
                incoming = counters()
                incoming[field] = 0
                sessions.update(incoming, True)
                self.assertEqual(sessions.revision, 1)
                self.assertEqual(sessions.live(), incoming)

    def test_fields_first_seen_later_use_zero_baseline(self):
        self.sessions.update({"elapsed": 100}, True)
        self.sessions.update({"elapsed": 101, "steps": 150}, True)
        self.assertEqual(self.sessions.live(), counters(101, 150, 0))

    def test_idle_does_not_create_session(self):
        self.sessions.update(counters(0, 0, 0), False)
        self.assertIsNone(self.sessions.current())
        self.assertFalse(Path(self.sessions.current_path).exists())

    def test_short_reboot_restores_baseline(self):
        self.sessions.update(counters(100, 500, 80), True)
        recovered = daemon.Sessions(self.path)
        recovered.update(counters(220, 600, 180), True)
        self.assertEqual(recovered.live(), counters(220, 600, 180))
        self.assertEqual(recovered.read(), [])

    def test_recovery_resolves_reset_while_idle(self):
        self.sessions.update(counters(), True)
        recovered = daemon.Sessions(self.path)
        recovered.update(counters(0, 0, 0), False)
        self.assertIsNone(recovered.current())
        self.assertEqual(recovered.read()[0]["duration"], 100)
        self.assertFalse(Path(recovered.current_path).exists())

    def test_recovery_preserves_paused_counters(self):
        self.sessions.update(counters(), True)
        recovered = daemon.Sessions(self.path)
        recovered.update(counters(), False)
        self.assertEqual(recovered.live(), counters())
        self.assertEqual(recovered.read(), [])

    def test_documented_long_gap_heuristic(self):
        self.sessions.update(counters(), True)
        recovered = daemon.Sessions(self.path)
        recovered.update(counters(3701, 5550, 3000), True)
        self.assertEqual(recovered.read()[0]["duration"], 100)
        self.assertEqual(recovered.live(), counters(3701, 5550, 3000))

    def test_stop_ignores_duplicates_and_trailing_moving_telemetry(self):
        self.sessions.update(counters(), True)
        self.sessions.finish()
        self.sessions.finish()
        self.sessions.update(counters(101, 152, 81), True)
        self.assertIsNone(self.sessions.session)
        self.assertEqual(len(self.sessions.read()), 1)
        self.assertEqual(self.sessions.revision, 1)
        self.sessions.update(counters(0, 0, 0), False)
        self.sessions.update(counters(1, 2, 1), True)
        self.assertEqual(self.sessions.live(), counters(1, 2, 1))

    def test_known_start_after_stop_uses_zero_baseline(self):
        self.sessions.update(counters(), True)
        self.sessions.finish()
        self.sessions.start()
        self.sessions.update(counters(200, 300, 160), True)
        self.assertEqual(self.sessions.live(), counters(200, 300, 160))

    def test_stop_before_first_sample_ignores_trailing_data(self):
        self.sessions.finish()
        self.sessions.update(counters(), True)
        self.assertIsNone(self.sessions.session)

    def test_zero_session_snapshot_is_removed_without_history_record(self):
        self.sessions.update(counters(0, 0, 0), True)
        self.assertTrue(Path(self.sessions.current_path).exists())
        self.sessions.finish()
        self.assertFalse(Path(self.sessions.current_path).exists())
        self.assertEqual(self.sessions.read(), [])

    def test_steps_only_session_is_recorded(self):
        self.sessions.update(counters(0, 5, 0), True)
        self.sessions.finish()
        self.assertEqual(self.sessions.read()[0]["steps"], 5)

    def test_history_reply_uses_current_memory_not_throttled_snapshot(self):
        self.sessions.update(counters(), True)
        self.sessions.update(counters(101, 152, 81), True)
        with open(self.sessions.current_path) as fh:
            self.assertEqual(json.load(fh)["session"]["duration"], 100)
        self.assertEqual(self.sessions.current()["session"]["duration"], 101)

    def test_persistence_disabled_still_accumulates(self):
        sessions = daemon.Sessions("")
        sessions.update(counters(), True)
        sessions.update(counters(101, 152, 81), True)
        self.assertEqual(sessions.live(), counters(101, 152, 81))

    def test_persistence_failure_does_not_disable_accounting(self):
        blocked = Path(self.directory.name) / "file"
        blocked.write_text("not a directory")
        sessions = daemon.Sessions(str(blocked / "history.jsonl"))
        sessions.update(counters(), True)
        self.assertTrue(sessions.failed)
        sessions.update(counters(101, 152, 81), True)
        self.assertEqual(sessions.live(), counters(101, 152, 81))

    def test_malformed_snapshots_are_ignored(self):
        for snapshot in ([], {"session": {}},
                         {"session": {"distance": 1, "duration": "bad", "steps": 1},
                          "prev": {}},
                         {"session": {"distance": 1, "duration": 1, "steps": 1},
                          "prev": {"elapsed": 65536}}):
            with self.subTest(snapshot=snapshot):
                Path(self.sessions.current_path).write_text(json.dumps(snapshot))
                recovered = daemon.Sessions(self.path)
                self.assertIsNone(recovered.session)
                self.assertEqual(recovered.previous, {})

    def test_relative_history_path(self):
        with contextlib.chdir(self.directory.name):
            sessions = daemon.Sessions("history.jsonl")
            sessions.update(counters(), True)
            sessions.finish()
            self.assertEqual(sessions.read()[0]["duration"], 100)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pad = make_pad(str(Path(self.directory.name) / "history.jsonl"))
        telemetry(self.pad)

    def test_pause_then_stop_and_duplicate_stop(self):
        self.pad.on_status(None, b"\x02\x02")
        self.assertEqual(self.pad.state["belt"], "paused")
        self.assertEqual(self.pad.sessions.read(), [])
        self.pad.on_status(None, b"\x02\x01")
        self.pad.on_status(None, b"\x02\x01")
        self.assertIsNone(self.pad.sessions.session)
        self.assertEqual(len(self.pad.sessions.read()), 1)
        self.assertEqual(self.pad.state["history_revision"], 1)

    def test_resume_pause_preserves_totals(self):
        self.pad.on_status(None, b"\x02\x02")
        self.pad.on_status(None, b"\x04")
        telemetry(self.pad, 101, 152, 81)
        self.assertEqual(self.pad.sessions.live(), counters(101, 152, 81))

    def test_missing_pause_parameter_preserves_session_until_reset(self):
        self.pad.on_status(None, b"\x02")
        self.assertIsNotNone(self.pad.sessions.session)
        telemetry(self.pad, 0, 0, 0, speed=0)
        self.assertEqual(len(self.pad.sessions.read()), 1)

    def test_reset_and_safety_stop_finalize(self):
        for frame in (b"\x01", b"\x03"):
            with self.subTest(frame=frame):
                pad = make_pad()
                telemetry(pad)
                pad.on_status(None, frame)
                self.assertIsNone(pad.sessions.session)
                self.assertEqual(pad.state["belt"], "stopped")

    def test_live_counters_do_not_wrap(self):
        pad = make_pad()
        telemetry(pad, 65535, 65535, 100)
        telemetry(pad, 0, 0, 101)
        self.assertEqual(pad.state["elapsed"], 65536)
        self.assertEqual(pad.state["steps"], 65536)

    def test_truncated_telemetry_does_not_mutate_state_or_accounting(self):
        state = dict(self.pad.state)
        self.pad.on_data(None, b"\x04\x24\x2c\x01\x00")
        self.assertEqual(self.pad.state, state)
        self.assertEqual(self.pad.sessions.live(), counters())

    def test_optional_fields_preserve_alignment(self):
        frame = (b"\xfe\x3f" + (300).to_bytes(2, "little")
                 + bytes(2) + (80).to_bytes(3, "little")
                 + bytes(4 + 4 + 1 + 1 + 5 + 1 + 1)
                 + (100).to_bytes(2, "little") + bytes(2 + 4)
                 + (150).to_bytes(2, "little"))
        self.assertEqual(daemon.parse_telemetry(frame), {"speed": 3.0, **counters()})

    def test_notification_without_speed_uses_last_known_speed(self):
        frame = (b"\x05\x24" + (81).to_bytes(3, "little")
                 + (101).to_bytes(2, "little") + (152).to_bytes(2, "little"))
        self.pad.on_data(None, frame)
        self.assertEqual(self.pad.state["elapsed"], 101)
        self.assertEqual(self.pad.state["steps"], 152)

    def test_reset_allows_telemetry_to_start_new_session(self):
        self.pad.on_status(None, b"\x01")
        telemetry(self.pad, 1, 2, 1)
        self.assertEqual(self.pad.sessions.live(), counters(1, 2, 1))

    def test_state_updates_do_not_finalize_or_accumulate(self):
        self.pad._set(belt="stopped")
        self.assertEqual(self.pad.sessions.live(), counters())
        self.assertEqual(self.pad.sessions.read(), [])


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_errors_are_quiet_but_changes_and_link_loss_are_logged(self):
        pad = make_pad()
        attempts = 0

        async def connect():
            nonlocal attempts
            attempts += 1
            if attempts == 6:
                raise asyncio.CancelledError()
            if attempts == 5:
                pad._set(connected=True)
            raise RuntimeError("pad unavailable" if attempts <= 2
                               else "connection lost")

        pad.connect_once = connect
        pad._idle_wait = AsyncMock(return_value=False)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(asyncio.CancelledError):
                await pad.run()
        self.assertEqual(output.getvalue().splitlines(), [
            "walkingpad: pad unavailable; reconnecting in 5s",
            "walkingpad: connection lost; reconnecting in 5s",
            "walkingpad: connection lost; reconnecting in 5s",
        ])
        self.assertEqual(pad._idle_wait.await_count, 5)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_wakes_sleeping_pad_and_preserves_other_mode_bits(self):
        pad = make_pad()
        pad.read_properties = AsyncMock(side_effect=[
            [{"id": 10, "value": 0x0240}],
            [{"id": 10, "value": 0x0200}],
        ])
        pad.write_property = AsyncMock(return_value=True)
        pad.cp = AsyncMock(return_value=4)
        self.assertEqual(await pad.cmd_start(), 4)
        pad.write_property.assert_awaited_once_with(10, 0x0200)
        pad.cp.assert_awaited_once_with(daemon.OP_START)

    async def test_start_does_not_change_manual_or_auto_mode(self):
        for value in (0x0200, 0x0220):
            with self.subTest(value=value):
                pad = make_pad()
                pad.read_properties = AsyncMock(return_value=[{"id": 10, "value": value}])
                pad.write_property = AsyncMock()
                self.assertTrue(await pad.wake_for_start())
                pad.write_property.assert_not_awaited()

    async def test_start_is_not_sent_when_wake_fails(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                pad = make_pad()
                pad.read_properties = AsyncMock(return_value=[{"id": 10, "value": 0x0240}])
                pad.write_property = AsyncMock(return_value=accepted)
                pad.cp = AsyncMock()
                self.assertEqual(await pad.cmd_start(), 4)
                pad.cp.assert_not_awaited()

    async def test_start_after_stop_without_status_notification(self):
        pad = make_pad()
        telemetry(pad)
        pad.on_status(None, b"\x02\x01")
        pad.cp = AsyncMock(return_value=1)
        pad.read_properties = AsyncMock(return_value=[])
        await pad.cmd_start()
        telemetry(pad, 1, 2, 1)
        self.assertEqual(pad.sessions.live(), counters(1, 2, 1))
        self.assertFalse(pad.state["starting"])
        pad.starting_task.cancel()

    async def test_failed_pause_does_not_suppress_subsequent_stop(self):
        pad = make_pad()
        telemetry(pad)
        pad.cp = AsyncMock(return_value=4)
        self.assertEqual(await pad.cmd_pause(), 4)
        self.assertEqual(pad.state["belt"], "started")
        pad.on_status(None, b"\x02\x01")
        self.assertIsNone(pad.sessions.session)

    async def test_pause_then_stop_without_notifications(self):
        pad = make_pad()
        telemetry(pad)
        pad.cp = AsyncMock(return_value=1)
        await pad.cmd_pause()
        self.assertIsNotNone(pad.sessions.session)
        await pad.cmd_stop()
        self.assertIsNone(pad.sessions.session)

    async def test_status_before_stop_reply_records_once(self):
        pad = make_pad()
        telemetry(pad)

        async def stop(*_args, **_kwargs):
            pad.on_status(None, b"\x02\x01")
            return 1

        pad.cp = stop
        await pad.cmd_stop()
        self.assertEqual(pad.sessions.revision, 1)

    async def test_queued_start_can_be_cancelled_while_disconnected(self):
        pad = make_pad()
        reply = await daemon.dispatch(pad, {"cmd": "start", "speed": 3})
        self.assertTrue(reply["ok"])
        reply = await daemon.dispatch(pad, {"cmd": "stop"})
        self.assertTrue(reply["ok"])
        self.assertIsNone(pad.pending_start)
        self.assertFalse(pad.state["starting"])
        pad.starting_task.cancel()

    async def test_new_start_replaces_previous_watchdog(self):
        pad = make_pad()
        pad.mark_starting()
        previous = pad.starting_task
        pad.mark_starting()
        await asyncio.sleep(0)
        self.assertTrue(previous.cancelled())
        pad.starting_task.cancel()

    async def test_history_serves_current_accounting(self):
        pad = make_pad()
        telemetry(pad)
        telemetry(pad, 101, 152, 81)
        reply = await daemon.dispatch(pad, {"cmd": "history"})
        self.assertEqual(reply["current"]["session"]["duration"], 101)
        self.assertEqual(reply["history_revision"], pad.state["history_revision"])
        pad.cp = AsyncMock(return_value=1)
        await pad.cmd_stop()
        finalized = await daemon.dispatch(pad, {"cmd": "history"})
        self.assertEqual(finalized["history_revision"], reply["history_revision"] + 1)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def wire_pad(self):
        pad = make_pad()
        pad._set(connected=True)
        pad.client = AsyncMock()
        pad._paced = AsyncMock()
        return pad

    async def test_stop_overtakes_unsent_work_even_during_pacing(self):
        pad = self.wire_pad()
        pacing, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def pace():
            pacing.set()
            await release.wait()

        async def write(char, frame, **kwargs):
            sent.append(frame[0])
            pad.on_cp(None, bytes([0x80, frame[0], 1]))

        pad._paced = pace
        pad.client.write_gatt_char.side_effect = write
        normal = asyncio.create_task(pad.cp(daemon.OP_REQUEST_CONTROL))
        await pacing.wait()
        stop = asyncio.create_task(pad.cmd_stop())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(normal, stop)
        self.assertEqual(sent, [daemon.OP_STOP_PAUSE, daemon.OP_REQUEST_CONTROL])

    async def test_stop_drains_active_exchange_then_discards_queued_speed(self):
        pad = self.wire_pad()
        active, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def write(char, frame, **kwargs):
            sent.append(frame[0])
            if len(sent) == 1:
                active.set()
                await release.wait()
            pad.on_cp(None, bytes([0x80, frame[0], 1]))

        pad.client.write_gatt_char.side_effect = write
        first = asyncio.create_task(pad.cmd_speed(2))
        await active.wait()
        queued = asyncio.create_task(pad.cmd_speed(4))
        await asyncio.sleep(0)
        stop = asyncio.create_task(pad.cmd_stop())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, queued, stop, return_exceptions=True)
        self.assertEqual(sent, [daemon.OP_SET_SPEED, daemon.OP_STOP_PAUSE])
        self.assertIsInstance(results[1], daemon.Superseded)

    async def test_only_latest_queued_speed_is_written(self):
        pad = self.wire_pad()
        pacing, release = asyncio.Event(), asyncio.Event()
        speeds = []

        async def pace():
            pacing.set()
            await release.wait()

        async def write(char, frame, **kwargs):
            speeds.append(int.from_bytes(frame[1:], "little"))
            pad.on_cp(None, bytes([0x80, frame[0], 1]))

        pad._paced = pace
        pad.client.write_gatt_char.side_effect = write
        old = asyncio.create_task(pad.cmd_speed(2))
        await pacing.wait()
        latest = asyncio.create_task(pad.cmd_speed(4))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(old, latest, return_exceptions=True)
        self.assertIsInstance(results[0], daemon.Superseded)
        self.assertEqual(speeds, [400])

    async def test_stop_cancels_start_while_waking(self):
        pad = self.wire_pad()
        waking, release = asyncio.Event(), asyncio.Event()

        async def wake():
            waking.set()
            await release.wait()
            return True

        pad.wake_for_start = wake
        pad.cp = AsyncMock(return_value=1)
        start = asyncio.create_task(pad.cmd_start())
        await waking.wait()
        await pad.cmd_stop()
        release.set()
        with self.assertRaises(daemon.Superseded):
            await start
        pad.cp.assert_awaited_once_with(daemon.OP_STOP_PAUSE, b"\x01", priority=0)
        self.assertFalse(pad.state["starting"])

    async def test_stop_cancels_delayed_start_speed(self):
        pad = self.wire_pad()
        waiting, release = asyncio.Event(), asyncio.Event()

        async def moving(*args):
            waiting.set()
            await release.wait()
            return True

        pad.wake_for_start = AsyncMock(return_value=True)
        pad._wait = moving
        pad.cp = AsyncMock(return_value=1)
        pad.cmd_speed = AsyncMock(return_value=1)
        start = asyncio.create_task(pad.cmd_start(4))
        await waiting.wait()
        await pad.cmd_stop()
        release.set()
        with self.assertRaises(daemon.Superseded):
            await start
        pad.cmd_speed.assert_not_awaited()

    async def test_failed_old_start_cannot_requeue_after_stop(self):
        pad = self.wire_pad()
        waiting, release = asyncio.Event(), asyncio.Event()

        async def start(*args):
            waiting.set()
            await release.wait()
            raise ConnectionError("link lost")

        pad.cmd_start = start
        pad.cmd_stop = AsyncMock(side_effect=pad.cancel_pending_motion)
        request = asyncio.create_task(daemon.dispatch(pad, {"cmd": "start"}))
        await waiting.wait()
        await daemon.dispatch(pad, {"cmd": "stop"})
        release.set()
        self.assertFalse((await request)["ok"])
        self.assertIsNone(pad.pending_start)

    async def test_offline_deadline_cancels_start_instead_of_hiding_it(self):
        pad = make_pad()
        with patch.object(daemon, "START_TIMEOUT", 0.01):
            await daemon.dispatch(pad, {"cmd": "start"})
            await asyncio.sleep(0.03)
        self.assertFalse(pad.state["starting"])
        self.assertIsNone(pad.pending_start)
        pad.cmd_start = AsyncMock()
        pad._resume_commands()
        await asyncio.sleep(0)
        pad.cmd_start.assert_not_awaited()

    async def test_unconfirmed_accepted_start_is_stopped_at_deadline(self):
        pad = self.wire_pad()
        pad.wake_for_start = AsyncMock(return_value=True)
        pad.cp = AsyncMock(return_value=1)
        with patch.object(daemon, "START_TIMEOUT", 0.01):
            self.assertEqual(await pad.cmd_start(), 1)
            await asyncio.sleep(0.03)
        self.assertFalse(pad.state["starting"])
        self.assertEqual([call.args[0] for call in pad.cp.await_args_list],
                         [daemon.OP_START, daemon.OP_STOP_PAUSE])

    async def test_offline_stop_is_sent_before_a_later_start(self):
        pad = make_pad()
        await daemon.dispatch(pad, {"cmd": "stop"})
        await daemon.dispatch(pad, {"cmd": "start"})
        epoch = pad.motion_epoch
        pad._set(connected=True)
        async def stop(*args, **kwargs):
            pad.on_status(None, b"\x02\x01")
            return 1

        pad.cp = AsyncMock(side_effect=stop)
        pad.cmd_start = AsyncMock(return_value=1)
        pad._resume_commands()
        await asyncio.sleep(0)
        pad.cp.assert_awaited_once_with(daemon.OP_STOP_PAUSE, b"\x01", priority=0)
        pad.cmd_start.assert_awaited_once_with(None, epoch)
        self.assertTrue(pad.state["starting"])
        self.assertFalse(pad.pending_stop)
        pad.starting_task.cancel()

    async def test_old_exchange_failure_cannot_break_replacement_connection(self):
        pad = self.wire_pad()
        sent = asyncio.Event()
        pad.client.write_gatt_char.side_effect = lambda *args, **kwargs: sent.set()
        old = asyncio.create_task(pad.cp(daemon.OP_REQUEST_CONTROL))
        await sent.wait()
        pad.break_link()
        pad.client = AsyncMock()
        pad.link_epoch += 1
        pad.link_broken = False
        pad._set(connected=True)

        async def write(char, frame, **kwargs):
            pad.on_cp(None, bytes([0x80, frame[0], 1]))

        pad.client.write_gatt_char.side_effect = write
        with self.assertRaises(ConnectionError):
            await old
        self.assertEqual(await pad.cp(daemon.OP_REQUEST_CONTROL), 1)
        self.assertTrue(pad.state["connected"])

    async def test_matching_stale_replies_during_pacing_are_ignored(self):
        pad = self.wire_pad()

        async def pace():
            pad.on_cp(None, bytes([0x80, daemon.OP_SET_SPEED, 4]))
            pad.on_vendor(None, daemon.ks_frame(0x72, 0x80, [2, 0, 60, 0x40]))

        async def write(char, frame, **kwargs):
            if char == daemon.FTMS_CP:
                pad.on_cp(None, bytes([0x80, frame[0], 1]))
            else:
                pad.on_vendor(None, daemon.ks_frame(0x72, 0x80, [2, 0, 30, 0xe0]))

        pad._paced = pace
        pad.client.write_gatt_char.side_effect = write
        self.assertEqual(await pad.cp(daemon.OP_SET_SPEED), 1)
        self.assertEqual(await pad.read_properties(), [{"id": 2, "value": 0xE01E}])

    async def test_timeout_invalidates_connection_and_queued_exchanges(self):
        pad = self.wire_pad()
        with self.assertRaises(asyncio.TimeoutError):
            await pad._cp_once(daemon.OP_SET_SPEED, timeout=0.001)
        self.assertFalse(pad.state["connected"])
        with self.assertRaises(ConnectionError):
            await pad.cp(daemon.OP_SET_SPEED)
        self.assertEqual(pad.client.write_gatt_char.await_count, 1)



class AutoStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_write_and_readback_with_unsolicited_notifications(self):
        pad = make_pad()
        pad._set(connected=True)
        changed = bytes.fromhex("72 50 03 02 1e e0 c5")
        replies = iter([
            "72 80 1c 01 00 00 00 04 00 15 00 05 00 00 00 02 "
            "00 3c 40 08 00 01 00 0a 00 00 02 06 00 00 00 c6",
            "72 81 02 02 00 f7",
            "72 80 1c 01 00 00 00 04 00 15 00 05 00 00 00 02 "
            "00 1e e0 08 00 01 00 0a 00 00 02 06 00 00 00 48",
        ])

        async def pace():
            # A change event can arrive after a waiter is installed, before
            # the paced request is sent. It must not complete that waiter.
            pad.on_vendor(None, changed)

        async def write(*args, **kwargs):
            pad.on_vendor(None, bytes.fromhex(next(replies)))
            pad.on_vendor(None, changed)

        pad._paced = pace
        pad.client = AsyncMock()
        pad.client.write_gatt_char.side_effect = write
        reply = await daemon.dispatch(pad, {
            "cmd": "auto-stop", "value": "on", "seconds": 30})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["auto_stop"]["raw"], 0xE01E)
        self.assertEqual(pad.client.write_gatt_char.await_count, 3)

    async def test_wake_write_rejection_does_not_queue_start(self):
        pad = make_pad()
        pad._set(connected=True)
        pad.cmd_start = AsyncMock(side_effect=daemon.PropertyWriteError("rejected"))
        reply = await daemon.dispatch(pad, {"cmd": "start"})
        self.assertFalse(reply["ok"])
        self.assertIsNone(pad.pending_start)

    async def test_wire_write_and_reply_errors(self):
        pad = make_pad()
        for data, error in [(b"\x02\x00", None), (b"\x02\x0a", "error 10"),
                            (b"\x08\x00", "unexpected property"),
                            (b"\x02", "unexpected property")]:
            with self.subTest(data=data):
                pad.vendor_request = AsyncMock(return_value=daemon.ks_frame(0x72, 0x81, data))
                if error:
                    with self.assertRaisesRegex(RuntimeError, error):
                        await pad.write_property(2, 0xE01E)
                else:
                    self.assertTrue(await pad.write_property(2, 0xE01E))
                pad.vendor_request.assert_awaited_once_with(
                    bytes.fromhex("72 01 03 02 1e e0 76"), expect=b"\x72\x81", timeout=3.0)

    def pad(self, *values):
        pad = make_pad()
        pad._set(connected=True)
        pad.read_properties = AsyncMock(side_effect=[
            [{"id": 2, "value": value}] for value in values])
        pad.write_property = AsyncMock(return_value=True)
        return pad

    async def test_query_observed_value(self):
        pad = self.pad(0x403C)
        reply = await daemon.dispatch(pad, {"cmd": "auto-stop"})
        self.assertEqual(reply, {"ok": True, "auto_stop": {
            "enabled": False, "supports_seconds": True,
            "custom_seconds": False, "seconds": 60, "raw": 0x403C}})
        pad.write_property.assert_not_awaited()

    async def test_enable_duration_and_disable_preserve_other_bits(self):
        for old, value, seconds, target in [
                (0x403C, "on", 30, 0xE01E),
                (0xE01E, "off", None, 0x601E),
                (0x601E, "on", None, 0xE01E)]:
            with self.subTest(value=value, seconds=seconds):
                pad = self.pad(old, target)
                reply = await daemon.dispatch(pad, {
                    "cmd": "auto-stop", "value": value, "seconds": seconds})
                self.assertTrue(reply["ok"])
                pad.write_property.assert_awaited_once_with(2, target)

    async def test_rejected_unsupported_missing_and_ineffective(self):
        for values, seconds, accepted in [
                ([0x403C], None, False), ([0x403C, 0x403C], None, True),
                ([60], 30, True), ([], None, True)]:
            with self.subTest(values=values, seconds=seconds):
                pad = self.pad(*values)
                if not values:
                    pad.read_properties = AsyncMock(return_value=[])
                pad.write_property.return_value = accepted
                reply = await daemon.dispatch(pad, {
                    "cmd": "auto-stop", "value": "on", "seconds": seconds})
                self.assertFalse(reply["ok"])
                self.assertIn("error", reply)
                if values == [60] or not values:
                    pad.write_property.assert_not_awaited()

    async def test_unchanged_setting_needs_no_write(self):
        pad = self.pad(0xE01E)
        self.assertTrue((await pad.auto_stop("on", 30))["ok"])
        pad.write_property.assert_not_awaited()
        self.assertEqual(pad.read_properties.await_count, 1)

    async def test_invalid_socket_requests_never_write(self):
        for value, seconds in [(None, 30), ("yes", None), ("off", 30), ("on", 0),
                               ("on", 8192), ("on", True), ("on", 1.5)]:
            pad = self.pad()
            reply = await daemon.dispatch(pad, {
                "cmd": "auto-stop", "value": value, "seconds": seconds})
            self.assertFalse(reply["ok"])
            pad.read_properties.assert_not_awaited()
            pad.write_property.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
