#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seat commands, pinned because every value here is counter-intuitive.

Measured against XPENG's own app, which is the only thing that says which *physical*
seat moved - the car's state fields report `driver_seat_heat`/`passenger_seat_heat`, so
they agree with whatever the car thinks, and that is exactly how this went wrong.

  * `cmd_type` **1 = driver, 0 = passenger**. The two were swapped here, because the
    values were assigned on a right-hand-drive car (where the driver's seat is the right
    one) by reading which state field moved, rather than the car's own meaning. A
    left-hand-drive owner setting "passenger" therefore heated his own seat.
  * `cmd_value` 1 = heating, 2 = **ventilation**. So the old map's "Medium" (2) turned
    ventilation on, which is what that owner saw.
  * `cmd_value` 0 and 3 come back "Nicht unterstützt für dieses Fahrzeug" - 0 is *not*
    "off", which is the error he hit pressing Off.

Only combinations measured, or the single outstanding candidate, are offered: a guess
here moves a real seat.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

SOURCE = pathlib.Path(_component.module_path("select.py")).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _seat_entities() -> dict[str, object]:
    """{entity key: cmd_type} for the seat selects this platform adds."""
    out: dict[str, object] = {}
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") != "XPengSeatHeatSelect":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        key = getattr(kw.get("key"), "value", None)
        if key:
            out[key] = getattr(kw.get("cmd_type"), "value", None)
    return out


def _commands() -> dict:
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SEAT_COMMANDS":
            return ast.literal_eval(node.value)
    raise AssertionError("SEAT_COMMANDS not found in select.py")


class SeatCommandTests(unittest.TestCase):
    def test_the_driver_is_one_and_the_passenger_is_zero(self):
        """The reverse of what this was, and the whole of the reported bug."""
        entities = _seat_entities()
        self.assertEqual(entities.get("driver_seat_heat_select"), 1)
        self.assertEqual(entities.get("passenger_seat_heat_select"), 0)

    def test_heating_and_ventilation_are_the_two_functions(self):
        commands = _commands()
        self.assertEqual(commands["Heat 1"], (1, 1))
        self.assertEqual(commands["Ventilation 1"], (1, 2))

    def test_off_is_not_the_value_that_was_refused(self):
        """`cmd_value` 0 as "off" is what produced his "not supported" error."""
        off = _commands()["Off"]
        self.assertNotEqual(off[1], 0)

    def test_no_offered_option_uses_a_refused_value(self):
        for option, (_mode, value) in _commands().items():
            self.assertIn(value, (1, 2), option)
            self.assertNotEqual(value, 3, option)

    def test_the_old_four_level_map_is_gone(self):
        """Low/Medium/High at 1/2/3 is what turned ventilation on by accident."""
        self.assertNotIn("Medium", SOURCE)
        self.assertNotIn("HEAT_OPTIONS = [\"Off\", \"Low\"", SOURCE)


if __name__ == "__main__":
    unittest.main()
