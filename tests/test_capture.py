#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the in-integration raw capture recorder.

    python3 tests/test_capture.py
    python3 -m unittest discover -s tests -v

The recorder is the replacement for the per-frame work that wedged Home Assistant
on 0.4.30, so the properties worth proving are the ones that make the wedge
impossible rather than the ones that make the happy path work:

* ``submit()`` never blocks and never raises, whatever the writer is doing;
* a full queue **drops and counts** instead of growing without bound;
* frames arrive in the journal byte for byte, with their arrival times;
* ``stop()`` drains what is queued, so a reload does not silently lose the tail;
* a frame submitted before ``start()`` or after ``stop()`` is a counted drop, not
  a lost frame and not an exception in paho's network thread.

It is deliberately Home-Assistant-free: the recorder imports no Home Assistant, so
these run with no stubs and no event loop.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "custom_components" / "xpeng"))
import _component  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "capture_under_test", _component.module_path("capture.py")
)
capture = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = capture
_SPEC.loader.exec_module(capture)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Recorder(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.recorder = capture.CaptureRecorder(self.dir.name, topics=["Xmart/test"])
        self.recorder.start()
        self.addCleanup(self.recorder.stop, 5.0, "cleanup")

    def _read(self):
        import can_journal

        return can_journal.JournalReader(self.recorder.current_file)

    def test_frames_are_written_byte_for_byte(self) -> None:
        payload = bytes(range(256))
        self.recorder.submit("Xmart/Car/94434/State/CanPack1s", payload)
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 1))
        self.recorder.stop(5.0, "test")
        found = list(self._read().messages())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].payload, payload)

    def test_arrival_times_are_kept(self) -> None:
        self.recorder.submit("t", b"a", ts_ns=1_700_000_000_000_000_000)
        self.recorder.submit("t", b"b", ts_ns=1_700_000_001_000_000_000)
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 2))
        self.recorder.stop(5.0, "test")
        found = list(self._read().messages())
        self.assertEqual(found[1].ts_ns - found[0].ts_ns, 1_000_000_000)

    def test_qos_and_retain_survive(self) -> None:
        self.recorder.submit("t", b"x", qos=1, retain=True)
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 1))
        self.recorder.stop(5.0, "test")
        found = list(self._read().messages())
        self.assertEqual((found[0].qos, found[0].retain), (1, True))

    def test_metadata_records_the_source(self) -> None:
        self.recorder.submit("t", b"x")
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 1))
        self.recorder.stop(5.0, "test")
        meta = self._read().scan().meta
        self.assertIn("MQTT connection", meta["source"])

    def test_stop_drains_what_is_queued(self) -> None:
        # A config-entry reload must not silently lose the tail: the frames are
        # already accepted, so they have to reach the file.
        for index in range(2000):
            self.recorder.submit("t", str(index).encode())
        summary = self.recorder.stop(10.0, "reload")
        self.assertEqual(summary["dropped"], 0)
        self.assertEqual(self.recorder.frames, 2000)
        self.assertEqual(len(list(self._read().messages())), 2000)

    def test_end_record_says_why_it_stopped(self) -> None:
        self.recorder.submit("t", b"x")
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 1))
        self.recorder.stop(5.0, "integration unloaded")
        end = self._read().scan().end_record
        self.assertEqual(end["reason"], "integration unloaded")

    def test_notes_reach_the_journal(self) -> None:
        # The integration records subscription results this way, so a topic the
        # broker refused is legible in the file afterwards.
        self.recorder.note("subscribe_refused", topic="Xmart/Tbox/+/CanPack1s",
                           reason="not authorized")
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 0))
        self.recorder.stop(5.0, "test")
        notes = [n["event"] for n in self._read().scan().notes]
        self.assertIn("subscribe_refused", notes)

    def test_note_before_start_is_harmless(self) -> None:
        recorder = capture.CaptureRecorder(self.dir.name + "-2")
        recorder.note("too_early")  # must not raise
        self.assertEqual(recorder.dropped, 0)

    def test_stats_describe_the_run(self) -> None:
        self.recorder.submit("t", b"12345")
        self.assertTrue(wait_for(lambda: self.recorder.frames >= 1))
        stats = self.recorder.stats()
        self.assertTrue(stats["running"])
        self.assertEqual(stats["frames"], 1)
        self.assertEqual(stats["payload_bytes"], 5)
        self.assertEqual(stats["dropped"], 0)


class NeverBlocks(unittest.TestCase):
    """The properties that stop this becoming the 0.4.30 failure again."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_a_full_queue_drops_and_counts(self) -> None:
        recorder = capture.CaptureRecorder(self.dir.name, queue_frames=4)
        recorder.start()
        # Stop the writer from draining, so the queue is guaranteed to fill.
        recorder._closing.set()
        recorder._thread.join(timeout=5.0)

        accepted = sum(1 for _ in range(100) if recorder.submit("t", b"x"))
        self.assertEqual(accepted, 0)
        self.assertEqual(recorder.dropped, 100)

    def test_submit_before_start_is_a_counted_drop_not_a_lost_frame(self) -> None:
        recorder = capture.CaptureRecorder(self.dir.name)
        self.assertFalse(recorder.submit("t", b"x"))
        self.assertEqual(recorder.dropped, 1)
        self.assertFalse(recorder.running)

    def test_submit_after_stop_is_a_counted_drop(self) -> None:
        recorder = capture.CaptureRecorder(self.dir.name)
        recorder.start()
        recorder.submit("t", b"x")
        recorder.stop(5.0, "test")
        self.assertFalse(recorder.submit("t", b"y"))
        self.assertEqual(recorder.dropped, 1)

    def test_submit_does_not_block_when_the_queue_is_full(self) -> None:
        # The MQTT callback thread must never wait on the disk.
        recorder = capture.CaptureRecorder(self.dir.name, queue_frames=2)
        recorder.start()
        recorder._closing.set()
        recorder._thread.join(timeout=5.0)

        started = time.monotonic()
        for _ in range(5000):
            recorder.submit("t", b"x" * 64)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "submit() blocked on a full queue")

    def test_concurrent_submitters_never_raise(self) -> None:
        recorder = capture.CaptureRecorder(self.dir.name, queue_frames=64)
        recorder.start()
        errors: list[BaseException] = []

        def hammer():
            try:
                for index in range(2000):
                    recorder.submit("t", str(index).encode(), qos=1)
            except BaseException as err:  # noqa: BLE001 - the point is that it cannot happen
                errors.append(err)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        summary = recorder.stop(10.0, "test")
        self.assertEqual(errors, [])
        # Every frame is either written or counted as dropped - never lost silently.
        self.assertEqual(summary["frames"] + summary["dropped"], 8000)


class Rotation(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_rotates_instead_of_growing_forever(self) -> None:
        recorder = capture.CaptureRecorder(
            self.dir.name, rotate_bytes=1024, max_bytes=0, queue_frames=4096
        )
        recorder.start()
        for _ in range(400):
            recorder.submit("t", b"x" * 32)
        recorder.stop(10.0, "test")
        self.assertGreater(len(recorder.parts), 1)
        for path in recorder.parts:
            self.assertTrue(os.path.exists(path))
        # Each part is independently readable, which is what rotation is for.
        import can_journal

        for path in recorder.parts:
            self.assertGreater(can_journal.JournalReader(path).scan().messages, 0)

    def test_size_cap_ends_the_capture_and_says_so(self) -> None:
        recorder = capture.CaptureRecorder(
            self.dir.name, rotate_bytes=0, max_bytes=512, queue_frames=4096
        )
        recorder.start()
        for _ in range(400):
            recorder.submit("t", b"x" * 32)
        summary = recorder.stop(10.0, "test")
        import can_journal

        notes = [n["event"] for n in can_journal.JournalReader(recorder.current_file).scan().notes]
        self.assertIn("capture_cap_reached", notes)
        # The cap is a stop, not a suggestion: it may overshoot by at most the
        # frame that crossed it, never by a whole batch.
        self.assertLess(summary["payload_bytes"], 512 + 64)


class OpenRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_falls_back_when_the_preferred_directory_is_unusable(self) -> None:
        # /share exists on HAOS and Supervised and not on a Container install.
        fallback = os.path.join(self.dir.name, "config", "xpeng-can")
        recorder, used = capture.open_recorder("/proc/nonexistent/xpeng-can", fallback)
        self.addCleanup(recorder.stop, 5.0, "cleanup")
        self.assertEqual(used, fallback)
        recorder.submit("t", b"x")
        self.assertTrue(wait_for(lambda: recorder.frames >= 1))

    def test_uses_the_preferred_directory_when_it_works(self) -> None:
        preferred = os.path.join(self.dir.name, "share", "xpeng-can")
        recorder, used = capture.open_recorder(preferred, os.path.join(self.dir.name, "fb"))
        self.addCleanup(recorder.stop, 5.0, "cleanup")
        self.assertEqual(used, preferred)

    def test_both_unusable_raises(self) -> None:
        with self.assertRaises(OSError):
            capture.open_recorder("/proc/nonexistent/a", "/proc/nonexistent/b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
