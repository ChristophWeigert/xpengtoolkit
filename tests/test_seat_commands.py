#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seat commands, pinned because every value here is counter-intuitive.

Measured against XPENG's own app, which is the only thing that says which *physical*
seat moved - the car's state fields report `driver_seat_heat`/`passenger_seat_heat`, so
they agree with whatever the car thinks, and that is exactly how this went wrong.

  * `cmd_type` **1 = driver, 0 = passenger, 3 = left rear, 4 = right rear**. The front
    two were swapped here, because the values were assigned on a right-hand-drive car
    (where the driver's seat is the right one) by reading which state field moved,
    rather than the car's own meaning. A left-hand-drive owner setting "passenger"
    therefore heated his own seat. The rear two were measured on 2026-10-14.
  * `cmd_value` 1 = heating, 2 = **ventilation**. So the old map's "Medium" (2) turned
    ventilation on, which is what that owner saw.
  * `cmd_value` 0 and 3 come back "Nicht unterstützt für dieses Fahrzeug" - 0 is *not*
    "off", which is the error he hit pressing Off.
  * `mode` is the **level** (0 = off, 1/2/3), not the momentary press this file used to
    describe. The press readings were taken ~2 s after each send, inside the car's 7-50 s
    apply/report window. See docs/SEAT_COMMANDS.md.

Only combinations measured are offered: a guess here moves a real seat.
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
                "options": getattr(kw.get("options"), "id", None),
                "commands": getattr(kw.get("commands"), "id", None),
                # Absent means enabled, which is HA's own default for the registry flag.
                "enabled_by_default": getattr(kw.get("enabled_by_default"), "value", True),
            }
    return out


def _constant(name: str):
    """The literal assigned to ``name`` in select.py."""
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in select.py")


def _commands(name: str) -> dict:
    """The ``{"Option": (mode, cmd_value)}`` table assigned to ``name``."""
    return _constant(name)


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

    def test_the_levels_are_the_mode(self):
        """Measured on the car: mode 1 read back 1, mode 2 read back 2, mode 0 read 0."""
        heat = _commands("SEAT_HEAT_COMMANDS")
        vent = _commands("SEAT_VENT_COMMANDS")
        self.assertEqual(heat["Off"], (0, 1))
        self.assertEqual(vent["Off"], (0, 2))
        self.assertEqual(heat["Heat 2"][0], 2)
        self.assertEqual(vent["Ventilation 2"][0], 2)

    def test_level_three_is_the_third_heat_level(self):
        """`mode 3` is level 3, not "off".

        The note this replaces ("mode 3 is accepted and turns the heating off, so it is
        not a level") was read ~2 s after the send, inside the car's own apply/report
        latency. Measured properly on 2026-10-14: `mode=3` from off reported 3.
        """
        self.assertEqual(_commands("SEAT_HEAT_COMMANDS")["Heat 3"], (3, 1))
        self.assertEqual(
            _constant("SEAT_HEAT_OPTIONS"), ["Off", "Heat 1", "Heat 2", "Heat 3"]
        )

    def test_ventilation_has_three_levels_too(self):
        """Measured 2026-10-14, after this file had pinned the opposite.

        `cmd_type 1, mode 3, cmd_value 2` made `driver_seat_ventilation` read 3 on data
        the car reported *after* the command (freshness gate: `last_data_timestamp`
        advanced 11:52:12 -> 11:52:32), and the car's own response carried
        `"driver_seat_ventilation": 3`. Ventilation kept two levels only because the
        correction that gave heating its third level was never applied to it - the same
        latency reading made `mode 3` look like "off" for both.
        """
        self.assertEqual(_commands("SEAT_VENT_COMMANDS")["Ventilation 3"], (3, 2))
        self.assertEqual(
            _constant("SEAT_VENT_OPTIONS"),
            ["Off", "Ventilation 1", "Ventilation 2", "Ventilation 3"],
        )
        # This used to assert that no rear entity mentioned ventilation, because a G6 has
        # no rear ventilation hardware. A G9 does, and `cmd_type 3, mode 1, cmd_value 2`
        # gave ventilation level 1 on its left rear - see the test below for how the two
        # entities are kept out of the way of cars that cannot use them.

    def test_the_rear_ventilation_pair_is_disabled_until_the_owner_enables_it(self):
        """Measured on a G9's left rear; the right is inferred from it by symmetry.

        The hardware is optional and nothing the integration reads says which car has it -
        `remoteCapabilities` is software flags and carries no seat entry - so the entities
        are registered disabled. Enabling them is a deliberate act by someone who can see
        the seat, which is the only place the answer exists.
        """
        entities = _seat_entities()
        for key, cmd_type, field in (
            ("left_rear_seat_ventilation_select", 3, "left_rear_seat_ventilation"),
            ("right_rear_seat_ventilation_select", 4, "right_rear_seat_ventilation"),
        ):
            self.assertEqual(entities[key]["cmd_type"], cmd_type, key)
            self.assertTrue(entities[key]["value_path"].endswith(field), key)
            self.assertEqual(entities[key]["commands"], "SEAT_VENT_COMMANDS", key)
            self.assertFalse(entities[key]["enabled_by_default"], key)
        # Everything measured on this car - including both rear heat controls - stays on.
        for key in (
            "driver_seat_heat_select",
            "left_rear_seat_heat_select",
            "right_rear_seat_heat_select",
        ):
            self.assertTrue(entities[key]["enabled_by_default"], key)

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

    def test_the_rear_seats_are_controls_at_their_measured_cmd_types(self):
        """3 and 4, each measured as a matched on/off pair on the same cmd_type.

        They were sensors-only before, because an unmeasured cmd_type can heat somebody:
        `5` is refused outright, and `2` moved the front-passenger field 46 s after the
        send, which is not a mapping anything needed.
        """
        entities = _seat_entities()
        self.assertEqual(entities["left_rear_seat_heat_select"]["cmd_type"], 3)
        self.assertEqual(entities["right_rear_seat_heat_select"]["cmd_type"], 4)
        self.assertTrue(
            entities["left_rear_seat_heat_select"]["value_path"].endswith(
                "left_rear_seat_heat"
            )
        )
        self.assertTrue(
            entities["right_rear_seat_heat_select"]["value_path"].endswith(
                "right_rear_seat_heat"
            )
        )
        # The car's own fields for them still exist, and are what the selects read.
        const = pathlib.Path(_component.module_path("const.py")).read_text(encoding="utf-8")
        for field in ("left_rear_seat_heat", "right_rear_seat_heat"):
            self.assertIn(field, const, field)

    def test_the_rear_seat_entities_are_the_four_controls(self):
        """Two measured heat controls and two ventilation controls registered disabled.

        This replaces an assertion that there were no rear ventilation controls at all:
        the G6 has no rear ventilation hardware, but the protocol does, and a G9 was
        measured. Optional hardware is why they are disabled rather than dropped.
        """
        rear = {key: spec for key, spec in _seat_entities().items() if "rear" in key}
        self.assertEqual(
            sorted(rear),
            [
                "left_rear_seat_heat_select",
                "left_rear_seat_ventilation_select",
                "right_rear_seat_heat_select",
                "right_rear_seat_ventilation_select",
            ],
        )

    def test_the_rear_seats_share_the_heat_level_map(self):
        """Their own entities, the same measured level map - level 3 included."""
        for key in ("left_rear_seat_heat_select", "right_rear_seat_heat_select"):
            self.assertEqual(_seat_entities()[key]["options"], "SEAT_HEAT_OPTIONS", key)
            self.assertEqual(_seat_entities()[key]["commands"], "SEAT_HEAT_COMMANDS", key)

    def test_the_seat_maps_no_longer_offer_the_mis_mapped_levels(self):
        """Low/Medium/High at 1/2/3 is what turned ventilation on by accident.

        Only the seats: the steering wheel (service 31) keeps its own 0-3 levels, which
        have not been measured and are left as they were.
        """
        seat_text = repr(_commands("SEAT_HEAT_COMMANDS")) + repr(_commands("SEAT_VENT_COMMANDS"))
        for gone in ("Low", "Medium", "High"):
            self.assertNotIn(gone, seat_text, gone)
        self.assertNotIn('HEAT_OPTIONS = ["Off", "Low"', SOURCE)


    def test_every_level_change_turns_the_seat_off_first(self):
        """`mode` is a selector, but the off-first step is kept for a measured reason.

        docs/SEAT_COMMANDS.md expects this step to be unnecessary now that `mode` reads
        as an assignment. It is kept anyway: no level change *from another level* (2 -> 3)
        has ever been measured, and "off, then the level" is the only order that has
        reproduced on a car. This test says so, so the next reader does not delete it as
        a cleanup.
        """
        self.assertIn("plan = [0, mode] if mode != 0 else [0]", SOURCE)
        self.assertIn("await asyncio.sleep(2)", SOURCE)

    def test_the_mode_is_recorded_as_a_level_selector(self):
        """Someone will otherwise re-read `mode 3` as "off" and drop the third level.

        That reading came from sampling ~2 s after the send, inside the car's 7-50 s
        apply/report window; docs/SEAT_COMMANDS.md has the paired send/transition evidence.
        """
        self.assertIn("docs/SEAT_COMMANDS.md", SOURCE)
        self.assertIn("the LEVEL, as a selector", SOURCE)


if __name__ == "__main__":
    unittest.main()
