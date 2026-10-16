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
        self.assertIn("value_fn=charge_cable_attached", block)
        self.assertNotIn("slow_charge_port_status ==", block)

    def test_the_port_sensor_reads_through_its_helper(self):
        block = self.SOURCE[self.SOURCE.index('key="slow_charge_port"'):]
        block = block[: block.index("XPengSensorEntityDescription", 10)]
        self.assertIn("value_fn=charge_port_state", block)
        self.assertIn('value_path="msg_content.instant.charge"', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
