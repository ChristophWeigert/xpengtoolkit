#!/usr/bin/env python3
"""Pin the mapping that made ``plugged_in`` read ON with nothing plugged in.

Measured on the car - G6, export, single CCS2 port - on 2026-10-17 with no cable
attached and the car awake for nine hours:

    slow_charge_port_status = 2   <- what plugged_in trusted: "Connected"
    fast_charge_port_status = 2   <- the same, permanently
    charge_gun_status       = 0   <- the physical connector: empty
    charge_connector_status = 7   <- that car's "unknown"
    charging_state          = 15

The first case below is that payload verbatim, so if anyone re-wires the sensor to
the port field again this file goes red. The plugged-side case (gun non-empty) is
the one thing here that was not observed on the car: it is stated as the assumption
it is, and it errs towards reporting plugged rather than not.

Run from the repo root:

    python3 tests/test_charge_port.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


port = _load("xpengtest.charge_port", _component.component_dir() / "charge_port.py")

#: ``msg_content.instant.charge`` as the car reported it, unplugged, 2026-10-17.
UNPLUGGED = {
    "slow_charge_port_status": 2,
    "fast_charge_port_status": 2,
    "charge_gun_status": 0,
    "charge_gun_lock_status": 0,
    "charge_connector_status": 7,
    "charging_state": 15,
    "battery_soc": 64,
}


class TestTheUnpluggedCar(unittest.TestCase):
    """The reported bug: no cable, and every port field claims one."""

    def test_no_cable_is_detected(self):
        self.assertFalse(port.charge_cable_attached(UNPLUGGED))

    def test_the_port_does_not_claim_a_cable(self):
        self.assertEqual(port.charge_port_state(UNPLUGGED), "Closed")

    def test_the_port_field_alone_cannot_say_connected(self):
        only_port_and_gun = {
            "slow_charge_port_status": 2,
            "charge_gun_status": 0,
        }
        self.assertFalse(port.charge_cable_attached(only_port_and_gun))
        self.assertEqual(port.charge_port_state(only_port_and_gun), "Closed")

    def test_a_shut_flap_is_still_closed(self):
        self.assertEqual(
            port.charge_port_state({**UNPLUGGED, "slow_charge_port_status": 0}),
            "Closed",
        )

    def test_an_open_flap_is_still_open(self):
        self.assertEqual(
            port.charge_port_state({**UNPLUGGED, "slow_charge_port_status": 1}),
            "Open",
        )


class TestThePluggedCar(unittest.TestCase):
    def test_a_gun_with_something_in_it_is_plugged(self):
        plugged = {**UNPLUGGED, "charge_gun_status": 1}
        self.assertTrue(port.charge_cable_attached(plugged))
        self.assertEqual(port.charge_port_state(plugged), "Connected")

    def test_charging_is_plugged_even_if_the_gun_field_disagrees(self):
        # The car's own claim that it is charging/connected/complete cannot be
        # wrong about there being a cable, whatever the connector code says.
        for state in (1, 2, 3):
            with self.subTest(charging_state=state):
                self.assertTrue(
                    port.charge_cable_attached(
                        {**UNPLUGGED, "charge_gun_status": 0, "charging_state": state}
                    )
                )

    def test_charging_is_plugged_even_on_a_car_with_no_physical_fields(self):
        self.assertTrue(port.charge_cable_attached({"charging_state": 2}))

    def test_a_recognised_connector_code_is_plugged(self):
        self.assertTrue(
            port.charge_cable_attached(
                {**UNPLUGGED, "charge_gun_status": 0, "charge_connector_status": 2}
            )
        )

    def test_the_unknown_connector_code_is_not_evidence(self):
        self.assertFalse(
            port.charge_cable_attached(
                {**UNPLUGGED, "charge_gun_status": 0, "charge_connector_status": 7}
            )
        )


class TestCarsWithoutThePhysicalFields(unittest.TestCase):
    """``plugged_in`` predates the gun fields, and those cars must not lose it."""

    def test_the_port_field_still_decides(self):
        self.assertTrue(port.charge_cable_attached({"slow_charge_port_status": 2}))
        self.assertFalse(port.charge_cable_attached({"slow_charge_port_status": 0}))

    def test_a_missing_section_is_not_plugged(self):
        self.assertFalse(port.charge_cable_attached(None))
        self.assertFalse(port.charge_cable_attached("nonsense"))
        self.assertIsNone(port.charge_port_state(None))


class TestTheDescriptionsUseTheMapping(unittest.TestCase):
    """Otherwise the helpers are correct and unused."""

    SOURCE = (_component.component_dir() / "const.py").read_text(encoding="utf-8")

    def test_plugged_in_reads_the_charge_section_through_the_helper(self):
        block = self.SOURCE[self.SOURCE.index('key="plugged_in"'):]
        block = block[: block.index("XPengBinarySensorEntityDescription", 10)]
        self.assertIn('value_path="msg_content.instant.charge"', block)
        self.assertIn("value_fn=charge_cable_state", block)
        self.assertNotIn("slow_charge_port_status ==", block)

    def test_the_port_sensor_reads_through_its_helper(self):
        block = self.SOURCE[self.SOURCE.index('key="slow_charge_port"'):]
        block = block[: block.index("XPengSensorEntityDescription", 10)]
        self.assertIn("value_fn=charge_port_state", block)
        self.assertIn('value_path="msg_content.instant.charge"', block)


class TestTheChargingState(unittest.TestCase):
    """Code 15 is not "Scheduled" on this car.

    The app's charge scheduler is disabled, the car is unplugged, and it reported
    15 for nine hours; on 2026-10-16 12:07 it alternated 15 with 2 while charge
    power was 5.2 kW. The label was a guess that read as a fact, so 15 is answered
    from the cable now, like plugged_in.
    """

    def test_15_with_no_cable_is_disconnected(self):
        self.assertEqual(port.charging_state_label(UNPLUGGED), "Disconnected")

    def test_15_with_a_cable_is_connected(self):
        self.assertEqual(
            port.charging_state_label({**UNPLUGGED, "charge_gun_status": 1}),
            "Connected",
        )

    def test_the_other_codes_are_untouched(self):
        for code, expected in (
            (0, "Disconnected"),
            (1, "Connected"),
            (2, "Charging"),
            (3, "Complete"),
            (4, "Error"),
            (8, "Idle"),
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    port.charging_state_label({**UNPLUGGED, "charging_state": code}),
                    expected,
                )

    def test_an_unknown_code_still_says_so(self):
        self.assertEqual(
            port.charging_state_label({**UNPLUGGED, "charging_state": 99}),
            "Unknown (99)",
        )

    def test_a_missing_code_is_not_a_state(self):
        self.assertIsNone(port.charging_state_label({}))
        self.assertIsNone(port.charging_state_label(None))

    def test_scheduled_is_no_longer_an_option(self):
        # It stays out of the enum: the sensor must not be able to report a state
        # the integration cannot justify.
        self.assertNotIn("Scheduled", port.CHARGING_STATES.values())
        self.assertNotIn(15, port.CHARGING_STATES)

    def test_an_armed_plan_is_scheduled(self):
        # The app's own wording, and the field that tracks it: repeat_type = 2.
        armed = {
            **UNPLUGGED,
            "repeat_type": 2,
            "reserve_charge_start_time": "22:00",
            # The plan charges to the limit, so the car reports this instead of an
            # end time. It must not be read as one, or crash anything.
            "reserve_charge_end_time": "31:63",
        }
        self.assertEqual(port.charging_state_label(armed), "Scheduled")
        self.assertTrue(port.charge_schedule_armed(armed))

    def test_a_plan_that_is_switched_off_is_not_scheduled(self):
        off = {**UNPLUGGED, "repeat_type": 0, "reserve_charge_start_time": "22:00"}
        self.assertEqual(port.charging_state_label(off), "Disconnected")
        self.assertFalse(port.charge_schedule_armed(off))

    def test_charging_outranks_the_plan(self):
        self.assertEqual(
            port.charging_state_label({**UNPLUGGED, "repeat_type": 2, "charging_state": 2}),
            "Charging",
        )

    def test_an_unobserved_repeat_pattern_still_counts_as_armed(self):
        for repeat in (1, 2, 7):
            with self.subTest(repeat_type=repeat):
                self.assertTrue(port.charge_schedule_armed({**UNPLUGGED, "repeat_type": repeat}))

    def test_a_car_without_the_field_has_no_plan(self):
        self.assertFalse(port.charge_schedule_armed({"charging_state": 15}))
        self.assertFalse(port.charge_schedule_armed(None))

    def test_scheduled_is_reportable_again(self):
        # It is a derived state now, so it has to be among the enum's options.
        self.assertIn("Scheduled", port.CHARGE_STATE_OPTIONS)
        self.assertIn("Charging", port.CHARGE_STATE_OPTIONS)

    def test_the_description_uses_the_label(self):
        source = (_component.component_dir() / "const.py").read_text(encoding="utf-8")
        block = source[source.index('translation_key="charging_state"') :]
        block = block[: block.index("XPengSensorEntityDescription", 10)]
        self.assertIn("value_fn=charging_state_label", block)
        self.assertIn('value_path="msg_content.instant.charge"', block)
        self.assertIn("options=list(CHARGE_STATE_OPTIONS)", block)


class TestTheBinarySensorRefusesToGuess(unittest.TestCase):
    """Plugged, waiting for an armed plan: the car says nothing, so say nothing.

    Measured 2026-10-17. The first two payloads below are byte-for-byte the same
    charge section; only the third, taken once the session woke, mentions a cable.
    """

    WAITING = {
        **UNPLUGGED,
        "repeat_type": 2,
        "reserve_charge_start_time": "22:00",
        "reserve_charge_end_time": "31:63",
    }
    AWAKE = {
        "charging_state": 1,
        "charge_gun_status": 4,
        "charge_connector_status": 7,
        "slow_charge_port_status": 0,
        "fast_charge_port_status": 2,
        "repeat_type": 0,
    }

    def test_no_cable_and_no_plan_is_false(self):
        self.assertIs(port.charge_cable_state(UNPLUGGED), False)

    def test_plugged_and_waiting_is_unknown(self):
        self.assertIsNone(port.charge_cable_state(self.WAITING))

    def test_a_woken_session_is_true(self):
        self.assertIs(port.charge_cable_state(self.AWAKE), True)

    def test_charging_is_true(self):
        self.assertIs(port.charge_cable_state({**self.AWAKE, "charging_state": 2}), True)

    def test_an_armed_plan_with_a_talking_car_is_not_a_cable(self):
        # The flag alone must not be an excuse for "unknown" any more: while the
        # car is reporting, its "no cable" is evidence. Measured 2026-10-17 - the
        # plan flag was still set after the app reported it disabled.
        self.assertIs(port.charge_cable_state(self.WAITING, silent_seconds=60), False)

    def test_an_armed_plan_with_a_quiet_car_is_unknown(self):
        self.assertIsNone(
            port.charge_cable_state(self.WAITING, silent_seconds=3 * 3600)
        )

    def test_the_silence_threshold_is_the_line(self):
        limit = port.CHARGE_CABLE_EVIDENCE_MAX_AGE
        self.assertIs(port.charge_cable_state(self.WAITING, silent_seconds=limit - 1), False)
        self.assertIsNone(port.charge_cable_state(self.WAITING, silent_seconds=limit + 1))

    def test_a_talking_unplugged_car_is_off_regardless(self):
        self.assertIs(port.charge_cable_state(UNPLUGGED, silent_seconds=60), False)
        self.assertIs(port.charge_cable_state(UNPLUGGED, silent_seconds=3 * 3600), False)

    def test_a_missing_section_is_unknown(self):
        self.assertIsNone(port.charge_cable_state(None))
        self.assertIsNone(port.charge_cable_state("nonsense"))

    def test_the_waiting_and_unplugged_payloads_really_are_identical(self):
        # If this ever stops being true, the unknown above is no longer needed -
        # and the test failing is how we would find out.
        cable_keys = [
            "charge_gun_status",
            "charge_connector_status",
            "slow_charge_port_status",
            "fast_charge_port_status",
            "charging_state",
        ]
        self.assertEqual(
            {k: UNPLUGGED[k] for k in cable_keys},
            {k: self.WAITING[k] for k in cable_keys},
        )


class TestTheEntitySuppliesTheSilence(unittest.TestCase):
    """The custom class is only used because the generic one cannot see silence."""

    BINARY_SOURCE = (_component.component_dir() / "binary_sensor.py").read_text(
        encoding="utf-8"
    )

    def test_the_class_passes_the_meaningful_silence(self):
        self.assertIn("class XPengPluggedInBinarySensor", self.BINARY_SOURCE)
        self.assertIn("silent_seconds=self._silent_seconds()", self.BINARY_SOURCE)
        self.assertIn('status.get("meaningful_silence_seconds")', self.BINARY_SOURCE)

    def test_it_polls_because_the_answer_changes_with_the_clock(self):
        # A car crossing the silence threshold sends nothing to notice it by, so an
        # unpolled entity would keep reporting whatever it said before.
        self.assertIn(
            "class XPengPluggedInBinarySensor(_PolledHealthBinarySensor, XPengBinarySensor)",
            self.BINARY_SOURCE,
        )

    def test_the_generic_entity_does_not_build_it(self):
        # ...or the payload-only answer would win half the time, depending on which
        # entity was added first.
        self.assertIn("if description is not plugged_in_description", self.BINARY_SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
