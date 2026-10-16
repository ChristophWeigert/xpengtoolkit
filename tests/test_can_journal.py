#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the offline CAN capture journal. Standard library only.

    python3 tests/test_can_journal.py
    python3 -m unittest discover -s tests -v

The journal is the artefact the whole recorder/decoder split rests on, so what is
tested here is the part that has to survive the real world rather than the happy
path:

* a truncated tail is tolerated rather than raised - a capture cut off by a kill
  is the normal case, and the frames before the cut are still evidence;
* a topic is written once and referenced by id afterwards, because the topic
  string is longer than most CAN payloads;
* ``--topic`` selectors behave the way a person typing one expects, since a
  selector that silently matches nothing looks exactly like an empty capture.

The format lives in the integration (``custom_components/xpeng/can_journal.py``)
because the integration writes captures too, and the tools load that one file by
path. So this loads it the way every other test loads its target - through
``_component.module_path`` - and separately checks that the tools' shim still
re-exports it, since a shim that drifts is a decoder that cannot read a capture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import _component  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "can_journal_under_test", _component.module_path("can_journal.py")
)
J = importlib.util.module_from_spec(_SPEC)
# Must be registered before exec: @dataclass resolves the defining module through
# sys.modules, and a module that is not there fails with an AttributeError on None.
sys.modules[_SPEC.name] = J
_SPEC.loader.exec_module(J)


def write_fixture(path: str, *, messages: int = 5) -> dict:
    writer = J.JournalWriter(path, {"tool": "test", "identity": {"vin": "TESTVIN"}})
    base = 1_700_000_000_000_000_000
    for index in range(messages):
        writer.message(
            "Xmart/Car/94434/State/CanPack1s",
            bytes([0xAA, 0x55, index]),
            ts_ns=base + index * 10**9,
        )
    writer.message("Xmart/MsgV2/User/1", b'{"a":1}', ts_ns=base, qos=1, retain=True)
    writer.note("subscribe_ok", topic="Xmart/Car/+/State/CanPack1s", granted=0)
    return writer.close(reason="test")


class RoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "c.xpgcan")
        self.summary = write_fixture(self.path)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_summary_counts_what_was_written(self) -> None:
        self.assertEqual(self.summary["messages"], 6)
        self.assertEqual(self.summary["topic_count"], 2)
        self.assertEqual(self.summary["payload_bytes"], 5 * 3 + 7)

    def test_messages_come_back_byte_for_byte(self) -> None:
        reader = J.JournalReader(self.path)
        found = list(reader.messages(topic_filter="Xmart/Car/94434"))
        self.assertEqual(len(found), 5)
        self.assertEqual(found[0].payload, bytes([0xAA, 0x55, 0]))
        self.assertEqual(found[4].payload, bytes([0xAA, 0x55, 4]))
        self.assertFalse(found[0].retain)

    def test_qos_and_retain_survive(self) -> None:
        found = list(J.JournalReader(self.path).messages(topic_filter="MsgV2"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].qos, 1)
        self.assertTrue(found[0].retain)

    def test_timestamp_survives(self) -> None:
        reader = J.JournalReader(self.path)
        found = list(reader.messages(topic_filter="Xmart/Car/94434", limit=2))
        self.assertEqual(found[1].ts_ns - found[0].ts_ns, 10**9)

    def test_zero_limit_means_all_not_one(self) -> None:
        # `limit=0` is what a CLI passes for "no limit"; reading it as a count
        # would return exactly one frame and look like a broken capture.
        found = list(J.JournalReader(self.path).messages(limit=0))
        self.assertEqual(len(found), 6)

    def test_scan_reports_end_record_and_notes(self) -> None:
        scan = J.JournalReader(self.path).scan()
        self.assertTrue(scan.meta["end_record_present"])
        self.assertEqual(scan.end_record["reason"], "test")
        self.assertEqual([n["event"] for n in scan.notes], ["subscribe_ok"])

    def test_scan_rates(self) -> None:
        scan = J.JournalReader(self.path).scan()
        stat = scan.stats["Xmart/Car/94434/State/CanPack1s"]
        # Five messages one second apart span four seconds.
        self.assertEqual(stat.count, 5)
        self.assertAlmostEqual(stat.as_dict()["rate_hz"], 1.25, places=3)

    def test_index_sidecar_is_written_and_resolves(self) -> None:
        index = self.path + ".index.json"
        self.assertTrue(os.path.exists(index))
        stored = json.load(open(index, encoding="utf-8"))
        self.assertEqual(stored["messages"], 6)
        # Pointing the reader at the sidecar reads the capture beside it.
        self.assertEqual(J.JournalReader(index).path, self.path)

    def test_close_is_idempotent(self) -> None:
        writer = J.JournalWriter(os.path.join(self.dir.name, "d.xpgcan"))
        writer.message("t", b"x")
        first = writer.close(reason="once")
        size_after_first = os.path.getsize(writer.path)
        second = writer.close(reason="twice")
        # A second END record would be silent corruption, and the recorder does
        # call close() from both a signal handler and its exit path.
        self.assertEqual(os.path.getsize(writer.path), size_after_first)
        self.assertEqual(second["messages"], first["messages"])

    def test_topic_written_once(self) -> None:
        # One TOPIC record per distinct topic, however many frames used it. The
        # name also appears in the END summary, so this counts records rather
        # than searching the bytes for the string.
        kinds = [rec_type for rec_type, _body in J.JournalReader(self.path).iter_records()]
        self.assertEqual(kinds.count(J.REC_TOPIC), 2)
        self.assertEqual(kinds.count(J.REC_MSG), 6)
        self.assertEqual(kinds.count(J.REC_NOTE), 1)


class BadInput(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_truncated_tail_is_tolerated(self) -> None:
        path = os.path.join(self.dir.name, "t.xpgcan")
        write_fixture(path)
        blob = pathlib.Path(path).read_bytes()
        pathlib.Path(path).write_bytes(blob[:-12])
        scan = J.JournalReader(path).scan()
        self.assertTrue(scan.truncated)
        self.assertGreater(scan.trailing_bytes, 0)
        # Everything before the cut is still readable.
        self.assertEqual(scan.messages, 6)
        self.assertFalse(scan.meta["end_record_present"])

    def test_clean_capture_reports_no_truncation(self) -> None:
        path = os.path.join(self.dir.name, "ok.xpgcan")
        write_fixture(path)
        self.assertFalse(J.JournalReader(path).scan().truncated)

    def test_missing_file_names_the_path(self) -> None:
        with self.assertRaises(J.JournalError) as caught:
            J.JournalReader(os.path.join(self.dir.name, "absent.xpgcan")).scan()
        self.assertIn("absent.xpgcan", str(caught.exception))

    def test_not_a_capture_is_refused(self) -> None:
        path = os.path.join(self.dir.name, "other.bin")
        open(path, "wb").write(b"this is not a capture at all, not even close")
        with self.assertRaises(J.JournalError) as caught:
            J.JournalReader(path).scan()
        self.assertIn("magic", str(caught.exception).lower())

    def test_is_capture(self) -> None:
        path = os.path.join(self.dir.name, "c.xpgcan")
        write_fixture(path)
        self.assertTrue(J.is_capture(path))
        self.assertFalse(J.is_capture(os.path.join(self.dir.name, "absent")))


class TopicSelectors(unittest.TestCase):
    """A selector that matches nothing is indistinguishable from an empty capture."""

    def test_mqtt_filter(self) -> None:
        self.assertTrue(J.topic_matches("Xmart/Car/+/State/CanPack1s", "Xmart/Car/9/State/CanPack1s"))
        self.assertTrue(J.topic_matches("Xmart/#", "Xmart/Car/9/State/CanPack1s"))
        self.assertFalse(J.topic_matches("Xmart/Car/+", "Xmart/Car/9/State/CanPack1s"))
        self.assertFalse(J.topic_matches("Xmart/Tbox/#", "Xmart/Car/9/State/CanPack1s"))

    def test_hash_matches_the_parent_itself(self) -> None:
        self.assertTrue(J.topic_matches("Xmart/Car/#", "Xmart/Car"))

    def test_path_prefix(self) -> None:
        self.assertTrue(J.topic_matches("Xmart/Car/94434", "Xmart/Car/94434/State/CanPack1s"))
        self.assertFalse(J.topic_matches("Xmart/Car/944", "Xmart/Car/94434/State/CanPack1s"))

    def test_bare_substring(self) -> None:
        self.assertTrue(J.topic_matches("MsgV2", "Xmart/MsgV2/User/1"))
        self.assertTrue(J.topic_matches("CanPack1s", "Xmart/Tbox/1/CanPack1s"))
        self.assertFalse(J.topic_matches("CanPack9s", "Xmart/Tbox/1/CanPack1s"))

    def test_empty_matches_everything(self) -> None:
        self.assertTrue(J.topic_matches("", "anything/at/all"))


class ToolsShim(unittest.TestCase):
    """The offline tools import ``xpeng_can_journal``; it must stay equivalent."""

    def test_shim_reexports_the_whole_public_surface(self):
        import xpeng_can_journal as shim

        for name in J.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(shim, name), f"the tools shim lost {name}")
                mine, theirs = getattr(J, name), getattr(shim, name)
                # The shim loads the same file as its own module object, so the
                # classes are equal in kind rather than identical - which is the
                # point: one implementation, two loaders.
                if callable(mine):
                    self.assertEqual(theirs.__name__, mine.__name__)
                else:
                    self.assertEqual(theirs, mine)

    def test_the_shim_reads_what_the_component_writes(self):
        import xpeng_can_journal as shim

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "c.xpgcan")
            writer = J.JournalWriter(path, {"tool": "cross-check"})
            writer.message("Xmart/Car/1/State/CanPack1s", b"\x01\x02")
            writer.close(reason="test")
            found = list(shim.JournalReader(path).messages())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].payload, b"\x01\x02")


if __name__ == "__main__":
    unittest.main(verbosity=2)
