"""Run with: python -m unittest discover -s tests."""

import argparse
import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock


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


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_after_stop_without_status_notification(self):
        pad = make_pad()
        telemetry(pad)
        pad.on_status(None, b"\x02\x01")
        pad.cp = AsyncMock(return_value=1)
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

        async def stop(*_args):
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


if __name__ == "__main__":
    unittest.main()
