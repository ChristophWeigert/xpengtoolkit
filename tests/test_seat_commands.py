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


def _seat_entities() -> dict[str, dict]:
    """{entity key: kwargs} for the seat selects this platform adds."""
    out: dict[str, dict] = {}
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") not in ("XPengSeatSelect", "XPengSeatHeatSelect"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        key = getattr(kw.get("key"), "value", None)
        if key:
            out[key] = {
                "cmd_type": getattr(kw.get("cmd_type"), "value", None),
                "value_path": getattr(kw.get("value_path"), "value", None),
            }
    return out


def _commands(name: str) -> dict:
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in select.py")


class SeatCommandTests(unittest.TestCase):
    def test_the_driver_is_one_and_the_passenger_is_zero(self):
        """The reverse of what this was, and the whole of the reported bug."""
        entities = _seat_entities()
        for key in (
            "driver_seat_heat_select",
            "driver_seat_ventilation_select",
        ):
            self.assertEqual(entities[key]["cmd_type"], 1, key)
        for key in (
            "passenger_seat_heat_select",
            "passenger_seat_ventilation_select",
        ):
            self.assertEqual(entities[key]["cmd_type"], 0, key)

    def test_each_function_reads_its_own_field(self):
        """Heating and ventilation are separate fields, so they are separate entities.

        Offering ventilation inside the heating select meant its result could never
        show: the heating field is truthful about heat, so it read back as Off.
        """
        entities = _seat_entities()
        self.assertTrue(entities["driver_seat_heat_select"]["value_path"].endswith("driver_seat_heat"))
        self.assertTrue(
            entities["driver_seat_ventilation_select"]["value_path"].endswith(
                "driver_seat_ventilation"
            )
        )
        self.assertTrue(
            entities["passenger_seat_ventilation_select"]["value_path"].endswith(
                "passenger_seat_ventilation"
            )
        )

    def test_heating_and_ventilation_are_the_two_functions(self):
        self.assertEqual(_commands("SEAT_HEAT_COMMANDS")["Heat 1"], (1, 1))
        self.assertEqual(_commands("SEAT_VENT_COMMANDS")["Ventilation 1"], (1, 2))

    def test_the_levels_are_the_mode_and_two_of_them_exist(self):
        """Measured on the car: mode 1 read back 1, mode 2 read back 2, mode 0 read 0."""
        self.assertEqual(_commands("SEAT_HEAT_COMMANDS")["Heat 2"], (2, 1))
        self.assertEqual(_commands("SEAT_VENT_COMMANDS")["Ventilation 2"], (2, 2))
        for table in ("SEAT_HEAT_COMMANDS", "SEAT_VENT_COMMANDS"):
            self.assertEqual(_commands(table)["Off"][0], 0, table)

    def test_level_three_is_not_offered(self):
        """mode 3 is accepted and turns the heating off, so it is not a level here."""
        for table in ("SEAT_HEAT_COMMANDS", "SEAT_VENT_COMMANDS"):
            for option, (mode, _value) in _commands(table).items():
                self.assertNotEqual(mode, 3, option)

    def test_off_is_not_the_value_that_was_refused(self):
        """`cmd_value` 0 as "off" is what produced the "not supported" error."""
        for table in ("SEAT_HEAT_COMMANDS", "SEAT_VENT_COMMANDS"):
            self.assertNotEqual(_commands(table)["Off"][1], 0, table)

    def test_no_offered_option_uses_a_refused_value(self):
        for table in ("SEAT_HEAT_COMMANDS", "SEAT_VENT_COMMANDS"):
            for option, (_mode, value) in _commands(table).items():
                self.assertIn(value, (1, 2), option)
                self.assertNotEqual(value, 3, option)

    def test_the_steering_wheel_keeps_its_own_value_space(self):
        """Service 31 is a different service; the seat map must not leak into it."""
        self.assertIn("STEER_VALUE_MAP", SOURCE)
        self.assertIn("STEER_OPTIONS", SOURCE)
        self.assertNotIn("_attr_options = HEAT_OPTIONS", SOURCE)
        self.assertIn("STEER_OPTIONS", SOURCE.split("class XPengSteeringWheelHeatSelect")[1])

    def test_the_rear_seats_are_sensors_not_controls(self):
        """Their cmd_type values have not been measured, and a wrong one heats somebody."""
        seat_keys = set(_seat_entities())
        for key in seat_keys:
            self.assertNotIn("rear", key, key)
        const = (pathlib.Path(_component.module_path("const.py"))).read_text(encoding="utf-8")
        for field in ("left_rear_seat_heat", "right_rear_seat_ventilation"):
            self.assertIn(field, const, field)

    def test_the_seat_maps_no_longer_offer_the_mis_mapped_levels(self):
        """Low/Medium/High at 1/2/3 is what turned ventilation on by accident.

        Only the seats: the steering wheel (service 31) keeps its own 0-3 levels, which
        have not been measured and are left as they were.
        """
        seat_text = repr(_commands("SEAT_HEAT_COMMANDS")) + repr(_commands("SEAT_VENT_COMMANDS"))
        for gone in ("Low", "Medium", "High"):
            self.assertNotIn(gone, seat_text, gone)
        self.assertNotIn('HEAT_OPTIONS = ["Off", "Low"', SOURCE)


if __name__ == "__main__":
    unittest.main()

    def test_every_level_change_turns_the_seat_off_first(self):
        """Because the switch is momentary, a press means "next", not "set".

        Sending the same payload three times read back 1, 0, 1, and pressing level 2
        straight from level 1 switched the heating off. "Off then press once" is the
        pattern that reproduced twice over for both levels, so it is used for every
        level change rather than reasoned about per transition.
        """
        self.assertIn("plan = [0, mode] if mode != 0 else [0]", SOURCE)
        self.assertIn("await asyncio.sleep(2)", SOURCE)

    def test_the_momentary_behaviour_is_recorded(self):
        """Someone will otherwise 'simplify' the off-first step away."""
        self.assertIn("momentary", SOURCE)
        self.assertIn("read back 1, 0, 1", SOURCE)
