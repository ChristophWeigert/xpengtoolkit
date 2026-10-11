#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The seat probe must keep saying what the measurements said.

It is the tool that lets anyone re-derive the mapping on another car, so the two facts
that cost the most to learn are pinned here: `cmd_type` 1 is the driver, and the car has
to be woken *after* a command - reading before that is what produced a series of wrong
"the command did nothing" conclusions.
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
    def test_the_seat_values_are_one_for_the_driver_and_zero_for_the_passenger(self):
        self.assertEqual(probe.SEAT_CMD_TYPE, {"driver": 1, "passenger": 0})

    def test_the_functions_are_one_for_heat_and_two_for_ventilation(self):
        self.assertEqual((probe.HEAT, probe.VENTILATION, probe.OFF), (1, 2, 0))

    def test_it_wakes_after_sending_not_before(self):
        """A sleeping car applies commands and reports nothing - the trap.

        Waking only at the start of a step makes every read the state as it was before
        the command arrived.
        """
        step = SOURCE[SOURCE.index("def step("):SOURCE.index("def main(")]
        self.assertLess(step.index("seat(core, token, cmd_type, mode, function)"),
                        step.index("wake(core, token)"))

    def test_it_prints_the_freshness_signal_that_actually_works(self):
        """Not a seat sensor's last_updated, which only moves when the value changes."""
        self.assertIn("last_data_update", SOURCE)


if __name__ == "__main__":
    unittest.main()
