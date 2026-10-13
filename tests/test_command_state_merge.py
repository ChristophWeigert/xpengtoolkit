#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A command's own acknowledgement is a state update. Standard library only.

    python3 tests/test_command_state_merge.py
    python3 -m unittest discover -s tests -v

Measured on a live car 2026-10-13, driving the charge-limit entity from Home
Assistant. The command went out at 20:51:01:

    Sent command: service_type=4 msg_content={'cmd_type': 2, 'cmd_value': 90}

and the answer arrived 1.4 seconds later:

    Command response: service_type=4 msg_type=1 content={
        "msg": "Erfolgreich", "code": 200, "charge_status": 15,
        "slow_charge_port_status": 2, "fast_charge_port_status": 2,
        "max_range_charge": 90}

Those keys are the ones ``instant.charge`` already holds, which is what makes
the acknowledgement usable as the confirmation: the charge limit entity reads
``msg_content.instant.charge.max_range_charge``, so folding the answer in is
what makes a change visible without waking the car.

Before that, the entity showed whatever the last full snapshot said. A drag that
landed in one of the watchdog's reconnect windows was dropped entirely (there is
exactly one ``Cannot send command: not connected`` in the log and no
``service_type=4`` publish at all), and the card re-rendered the old number.
What is pinned here is the merging half; ``number.py`` is not importable without
a full Home Assistant, so the entity's read path is asserted structurally.

``coordinator.py`` is loaded with stubbed Home Assistant modules, because the
behaviour under test is a method on a class that inherits one.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

PKG_DIR = _component.component_dir()
_component.assert_is_default_target()


# -- a Home Assistant just big enough to import coordinator.py -------------

class _CoordinatorBase:
    """Stand-in for DataUpdateCoordinator (subscriptable, never constructed)."""

    def __class_getitem__(cls, item):  # noqa: N805
        return cls

    def __init__(self, *args, **kwargs):
        pass

    def async_set_updated_data(self, data):
        self.data = data


def _install_ha_stubs():
    ha = types.ModuleType("homeassistant")

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn

    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = type("Store", (), {})
    upd = types.ModuleType("homeassistant.helpers.update_coordinator")
    upd.DataUpdateCoordinator = _CoordinatorBase

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.storage"] = storage
    sys.modules["homeassistant.helpers.update_coordinator"] = upd
    ha.core = core
    ha.helpers = helpers


def _load_coordinator():
    """Load the real coordinator.py against the stubs above."""
    _install_ha_stubs()

    pkg = types.ModuleType("custom_components")
    pkg.__path__ = []
    xpkg = types.ModuleType("custom_components.xpeng")
    xpkg.__path__ = [str(PKG_DIR)]
    sys.modules["custom_components"] = pkg
    sys.modules["custom_components.xpeng"] = xpkg

    # The real const.py needs HA's entity-description dataclasses; the
    # coordinator only wants DOMAIN out of it.
    const = types.ModuleType("custom_components.xpeng.const")
    const.DOMAIN = "xpeng"
    sys.modules["custom_components.xpeng.const"] = const

    for name in ("command_policy", "vin_descriptor", "coordinator"):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.xpeng.{name}", PKG_DIR / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"custom_components.xpeng.{name}"] = mod
        spec.loader.exec_module(mod)

    return sys.modules["custom_components.xpeng.coordinator"]


coordinator = _load_coordinator()
CHARGE = coordinator.CHARGE_SERVICE_TYPE


def _coordinator_with(cached):
    """An instance with just the attributes the merge touches."""
    coord = object.__new__(coordinator.XPengDataCoordinator)
    coord.vin = "L1NNSGHA3SB800783"
    coord._last_full_data = cached
    return coord


def _cached_state():
    return {
        "msg_content": {
            "instant": {
                "charge": {
                    "battery_soc": 23,
                    "max_range_charge": 90,
                    "charging_state": "Scheduled",
                },
                "door": {"driver_door_lock_st": 1},
            },
            "interval": {"charge": {"power": 0}},
        },
        "vin": "L1NNSGHA3SB800783",
    }


def _charge_ack(**content):
    """The shape mqtt_client._fire_command_feedback hands the coordinator."""
    body = {"msg": "Erfolgreich", "code": 200}
    body.update(content)
    return {
        "service_type": CHARGE,
        "msg_type": 1,
        "msg_id": "11103488591791885061448008",
        "code": 200,
        "message": "Erfolgreich",
        "content": body,
    }


#: The exact payload the live car answered a set-charge-limit with.
LIVE_ACK = dict(
    charge_status=15,
    slow_charge_port_status=2,
    fast_charge_port_status=2,
    battery_keep_warm_status=0,
    max_range_charge=90,
)


class TestAChargeAckUpdatesTheState(unittest.TestCase):
    def test_the_acknowledgement_moves_the_charge_limit(self):
        coord = _coordinator_with(_cached_state())
        self.assertTrue(
            coord._merge_command_state(
                _charge_ack(**{**LIVE_ACK, "max_range_charge": 100})
            )
        )

        charge = coord._last_full_data["msg_content"]["instant"]["charge"]
        self.assertEqual(charge["max_range_charge"], 100)
        # Every other field the car reported comes with it...
        self.assertEqual(charge["charge_status"], 15)
        self.assertEqual(charge["slow_charge_port_status"], 2)
        self.assertEqual(charge["fast_charge_port_status"], 2)
        self.assertEqual(charge["battery_keep_warm_status"], 0)

    def test_fields_the_ack_does_not_carry_are_kept(self):
        coord = _coordinator_with(_cached_state())
        coord._merge_command_state(_charge_ack(max_range_charge=100))

        instant = coord._last_full_data["msg_content"]["instant"]
        self.assertEqual(instant["charge"]["battery_soc"], 23)
        self.assertEqual(instant["charge"]["charging_state"], "Scheduled")
        # Other sections and the interval block are untouched.
        self.assertEqual(instant["door"], {"driver_door_lock_st": 1})
        self.assertEqual(
            coord._last_full_data["msg_content"]["interval"],
            {"charge": {"power": 0}},
        )
        self.assertEqual(coord._last_full_data["vin"], coord.vin)

    def test_the_entity_read_path_answers_the_new_limit(self):
        """``XPengChargeLimit.native_value`` walks exactly this path."""
        coord = _coordinator_with(_cached_state())
        coord._merge_command_state(_charge_ack(max_range_charge=95))

        value = (
            coord._last_full_data["msg_content"]["instant"]["charge"]
            .get("max_range_charge")
        )
        self.assertEqual(value, 95)

    def test_code_and_msg_are_not_merged_as_charge_fields(self):
        coord = _coordinator_with(_cached_state())
        coord._merge_command_state(_charge_ack(max_range_charge=100))

        charge = coord._last_full_data["msg_content"]["instant"]["charge"]
        self.assertNotIn("code", charge)
        self.assertNotIn("msg", charge)

    def test_the_cached_state_object_is_replaced_not_mutated_in_place(self):
        """The coordinator re-publishes by identity; a new dict is the signal."""
        cached = _cached_state()
        coord = _coordinator_with(cached)
        coord._merge_command_state(_charge_ack(max_range_charge=100))

        self.assertIsNot(coord._last_full_data, cached)
        self.assertEqual(
            cached["msg_content"]["instant"]["charge"]["max_range_charge"], 90
        )


class TestWhatIsNotMerged(unittest.TestCase):
    def test_a_refusal_is_not_state(self):
        coord = _coordinator_with(_cached_state())
        ack = _charge_ack(max_range_charge=100)
        ack["code"] = 500
        ack["content"]["code"] = 500
        self.assertFalse(coord._merge_command_state(ack))
        self.assertEqual(
            coord._last_full_data["msg_content"]["instant"]["charge"][
                "max_range_charge"
            ],
            90,
        )

    def test_another_subsystems_response_is_not_charge_state(self):
        """Service 9 (door) answers with door fields, not charge fields."""
        coord = _coordinator_with(_cached_state())
        ack = _charge_ack(max_range_charge=100)
        ack["service_type"] = 9
        self.assertFalse(coord._merge_command_state(ack))
        self.assertEqual(
            coord._last_full_data["msg_content"]["instant"]["charge"][
                "max_range_charge"
            ],
            90,
        )

    def test_a_response_with_nothing_but_a_status_is_not_merged(self):
        coord = _coordinator_with(_cached_state())
        ack = _charge_ack()
        ack["content"] = {"code": 200, "msg": "Erfolgreich"}
        self.assertFalse(coord._merge_command_state(ack))

    def test_a_malformed_response_is_not_merged(self):
        coord = _coordinator_with(_cached_state())
        ack = _charge_ack(max_range_charge=100)
        ack["content"] = "Erfolgreich"
        self.assertFalse(coord._merge_command_state(ack))

    def test_nothing_cached_means_nothing_to_merge_into(self):
        """A command ack must not become the first 'full' snapshot.

        Sensors read `msg_content.instant.*`; publishing a snapshot built from
        one command's answer would blank every field that command did not
        mention.
        """
        coord = _coordinator_with(None)
        self.assertFalse(coord._merge_command_state(_charge_ack(max_range_charge=100)))
        self.assertFalse(coord._last_full_data)


class TestOnlyTheChargeSubsystem(unittest.TestCase):
    def test_the_charge_service_type_is_four(self):
        """Pinned against services.py: service_type 4 is the charge port and
        the charge limit (cmd_type 2), which is why its ack carries the
        instant.charge fields."""
        self.assertEqual(CHARGE, 4)

    def test_the_live_acknowledgement_is_recognised(self):
        coord = _coordinator_with(_cached_state())
        self.assertTrue(coord._merge_command_state(_charge_ack(**LIVE_ACK)))
        charge = coord._last_full_data["msg_content"]["instant"]["charge"]
        self.assertEqual(charge["max_range_charge"], 90)
        self.assertEqual(charge["charge_status"], 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
