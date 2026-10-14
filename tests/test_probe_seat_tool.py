#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The seat probe must keep saying what the measurements said.

It is the tool that lets anyone re-derive the mapping on another car, so the facts
that cost the most to learn are pinned here: `cmd_type` 1 is the driver, 3 and 4 are the
rear seats (measured 2026-10-14, docs/SEAT_COMMANDS.md), the car has to be woken *after*
a command - reading before that is what produced a series of wrong "the command did
nothing" conclusions - and a seat whose value nobody has measured is still refused
rather than guessed at.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

# tools/ is not the component tree, so this resolves from the repository root rather
# than through _component.module_path().
PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "probe_seat.py"
SOURCE = PATH.read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("probe_seat_t", PATH)
probe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["probe_seat_t"] = probe
spec.loader.exec_module(probe)


class ProbeSeatTests(unittest.TestCase):
    def test_the_seat_values_are_measured_per_seat(self):
        self.assertEqual(
            probe.SEAT_CMD_TYPE,
            {"driver": 1, "passenger": 0, "rear-left": 3, "rear-right": 4},
        )

    def test_the_functions_are_one_for_heat_and_two_for_ventilation(self):
        self.assertEqual((probe.HEAT, probe.VENTILATION, probe.OFF), (1, 2, 0))

    def test_it_wakes_after_sending_not_before(self):
        """A sleeping car applies commands and reports nothing - the trap.

        Waking only at the start of a step makes every read the state as it was before
        the command arrived.
        """
        step = SOURCE[SOURCE.index("def step("):SOURCE.index("def main(")]
        self.assertLess(step.index("seat(core, token, cmd_type, mode, function)"),
                        step.index("wait_for_fresh(core, token, vin_slug, label, settle, previous)"))
        waited = SOURCE[SOURCE.index("def wait_for_fresh("):SOURCE.index("def step(")]
        self.assertLess(waited.index("wake(core, token)"), waited.index("state = show("))

    def test_a_stale_reading_is_not_a_result(self):
        """The timestamp must advance past the read taken before the command.

        The run of 2026-10-14 read "moved: nothing" off a report from six hours earlier,
        and read a reconnect's restored zeros as a seat turning off.
        """
        before = {field: "0" for field in probe.ALL_SEAT_FIELDS}
        before[probe.FRESHNESS_FIELD] = "2026-10-14T04:41:23+00:00"

        self.assertFalse(probe.is_fresh(dict(before), before), "the same timestamp is not fresh")

        newer = dict(before, **{probe.FRESHNESS_FIELD: "2026-10-14T10:31:01+00:00"})
        self.assertTrue(probe.is_fresh(newer, before))

        reconnecting = dict(newer, left_rear_seat_heating="unavailable")
        self.assertFalse(probe.is_fresh(reconnecting, before), "a reconnect is not a reading")

        self.assertTrue(probe.is_fresh(newer, None), "a baseline only has to be readable")

    def test_it_prints_the_freshness_signal_that_actually_works(self):
        """`..._last_data_timestamp`, not a seat sensor's `last_updated`.

        A seat field resting at 0 keeps an old `last_updated` however much data arrives,
        so the car's own freshness signal is printed beside it. Both
        `..._last_data_timestamp` and the older `..._last_data_update` exist and carry
        the same coordinator field; the timestamp one is the machine-readable entity.
        """
        self.assertEqual(probe.FRESHNESS_FIELD, "last_data_timestamp")
        self.assertIn('FRESHNESS_FIELD = "last_data_timestamp"', SOURCE)

    def test_the_rear_seats_are_watched_through_the_cars_own_fields(self):
        self.assertEqual(probe.SEAT_FIELDS["rear-left"],
                         ("left_rear_seat_heating", "left_rear_seat_ventilation"))
        self.assertEqual(probe.SEAT_FIELDS["rear-right"],
                         ("right_rear_seat_heating", "right_rear_seat_ventilation"))

    def test_every_seat_field_is_read_not_only_the_chosen_seats(self):
        """A candidate `cmd_type` may move a seat nobody expected."""
        for pair in probe.SEAT_FIELDS.values():
            for field in pair:
                self.assertIn(field, probe.ALL_SEAT_FIELDS)

    def test_the_rear_values_are_the_measured_ones(self):
        """3 and 4, measured 2026-10-14 as matched on/off pairs (docs/SEAT_COMMANDS.md).

        Each moved its own rear field and left the other rear seat alone. `2` stays out
        of the map: it was accepted once and moved the *front passenger* field 46 s after
        the send. `5` is refused outright (`code 13023`).
        """
        self.assertEqual(probe.SEAT_CMD_TYPE["rear-left"], 3)
        self.assertEqual(probe.SEAT_CMD_TYPE["rear-right"], 4)
        self.assertNotIn(2, probe.SEAT_CMD_TYPE.values())
        self.assertNotIn(5, probe.SEAT_CMD_TYPE.values())

    def test_the_candidates_stay_whole_for_cars_that_differ(self):
        """3 and 4 are this car's answer, so the scan keeps them: another car may not.

        What the candidate list must never carry is either measured *front* value - a
        candidate that collides with a known value is not a candidate.
        """
        self.assertNotIn(probe.SEAT_CMD_TYPE["driver"], probe.REAR_CMD_TYPE_CANDIDATES)
        self.assertNotIn(probe.SEAT_CMD_TYPE["passenger"], probe.REAR_CMD_TYPE_CANDIDATES)
        self.assertEqual(probe.REAR_CMD_TYPE_CANDIDATES, (2, 3, 4, 5))

    def test_a_seat_with_no_measured_cmd_type_is_still_refused(self):
        """The guard that kept the rear seats out of reach until they were measured.

        All four seats this tool knows are measured now, so the reachable case is a seat
        added to SEAT_FIELDS before its value is known - a third row, say. That must
        still refuse rather than send a guess; an explicit `--cmd-type`, or the scan,
        remains the way through.
        """
        with self.assertRaises(ValueError):
            probe.resolve_cmd_types("third-row")
        self.assertEqual(probe.resolve_cmd_types("rear-left"), [3])
        self.assertEqual(probe.resolve_cmd_types("rear-right"), [4])
        self.assertEqual(probe.resolve_cmd_types("rear-left", cmd_type=5), [5])
        self.assertEqual(probe.resolve_cmd_types("rear-left", scan=True),
                         list(probe.REAR_CMD_TYPE_CANDIDATES))
        self.assertEqual(probe.resolve_cmd_types("driver"), [1])
        self.assertEqual(probe.resolve_cmd_types("passenger"), [0])

    def test_a_dead_session_is_refused_rather_than_measured_through(self):
        """`polling_status: Reconnecting` means no reading below it means anything.

        The 2026-10-14 run on .236 produced a confident-looking "moved: nothing" and a
        phantom off-transition while the host was reconnecting throughout.
        """
        self.assertTrue(probe.integration_is_usable("Running", "on"))
        self.assertTrue(probe.integration_is_usable("n/a", "on"), "older integrations have no status entity")
        self.assertFalse(probe.integration_is_usable("Reconnecting", "on"))
        self.assertFalse(probe.integration_is_usable("Running", "off"))
        self.assertFalse(probe.integration_is_usable("Running", "unavailable"))
        self.assertIn("--force", SOURCE)

    def test_the_sequence_reaches_three_bars(self):
        """Level 3 is a real level: measured 2026-10-14, `mode 3` from off reported 3.

        The earlier "mode 3 turns the heating off" reading was taken ~2 s after the send,
        inside the car's 7-50 s apply/report window. Every press is still preceded by an
        off - that is the only order that has reproduced on a car.
        """
        self.assertEqual(probe.LEVELS, (1, 2, 3))
        plan = probe.sequence_plan()
        self.assertEqual([mode for mode, _function, _label in plan],
                         [probe.OFF, 1, probe.OFF, 2, probe.OFF, 3, probe.OFF] * 2)
        self.assertEqual([function for _mode, function, _label in plan],
                         [probe.HEAT] * 7 + [probe.VENTILATION] * 7)
        for index, (mode, _function, _label) in enumerate(plan):
            if mode in probe.LEVELS:
                self.assertEqual(plan[index - 1][0], probe.OFF, "a press with no off before it")


if __name__ == "__main__":
    unittest.main()
