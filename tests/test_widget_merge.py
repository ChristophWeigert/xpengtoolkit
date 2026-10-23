#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The REST fallback merge must survive the partial frames that follow it.

    python3 tests/test_widget_merge.py
    python3 -m unittest discover -s tests -v

Measured on the reference G6, 2026-10-23. The push fan-out wedged at 17:49:42
with SOC 50; the REST widget fallback then wrote the live 49% at 19:27:10,
19:42:56 and 19:58:41. Between those merges a charge-less partial frame - a
door ping, or one of the car's own short acks - merged onto
``mqtt_client._last_full_content``, which still held the frozen 50, and pushed
that whole snapshot back through the coordinator. The battery and range
sensors flipped 49/50 and 267/269 for an hour.

The merge happens in the coordinator, which is invisible to this client, so
the client's baseline has to be taught what the merge decided. ``adopt_state``
is that lesson; these tests pin the consequence, using the same synthetic
frames as ``test_mqtt_state_delivery.py``.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402
from test_mqtt_state_delivery import _client, _Msg, _state  # noqa: E402

_component.assert_is_default_target()


def _adopt(client, soc, *, odo=62234):
    """Hand the client the coordinator's merged state, as set_baseline_sink does."""
    content = {
        "code": 200,
        "instant": {
            "charge": {"battery_soc": soc, "charge_power": 0},
            "odometer": {"total_mileage": odo},
        },
        "interval": {"charge": {"power": 0, "electricity": 0}},
    }
    client.adopt_state(copy.deepcopy(content))
    return content


class ThePartialKeepsTheMerge(unittest.TestCase):
    def test_a_charge_less_partial_does_not_restore_the_stale_soc(self):
        """The measured flip: 49 merged, then a door ping re-broadcasts 50."""
        client, calls = _client()
        _adopt(client, 49)
        client._on_message(
            None, None, _Msg(_state(instant={"door": {"driver_door_lock_st": 1}}))
        )
        self.assertEqual(len(calls), 1)
        charge = calls[-1]["msg_content"]["instant"]["charge"]
        self.assertEqual(charge["battery_soc"], 49)

    def test_a_charge_carrying_partial_still_wins(self):
        """The car sending a real charge reading is still the car talking."""
        client, calls = _client()
        _adopt(client, 49)
        client._on_message(
            None, None, _Msg(_state(instant={"charge": {"battery_soc": 48}}))
        )
        self.assertEqual(
            calls[-1]["msg_content"]["instant"]["charge"]["battery_soc"], 48
        )

    def test_the_adopted_odometer_is_kept(self):
        client, calls = _client()
        _adopt(client, 49, odo=62234)
        client._on_message(
            None, None, _Msg(_state(instant={"door": {"driver_door_lock_st": 1}}))
        )
        odometer = calls[-1]["msg_content"]["instant"]["odometer"]
        self.assertEqual(odometer["total_mileage"], 62234)

    def test_an_ack_equal_to_the_adopted_state_is_not_data(self):
        """The adopted state becomes the baseline the stub test compares against."""
        client, calls = _client()
        adopted = _adopt(client, 49)
        client._on_message(None, None, _Msg(_state(interval=adopted["interval"])))
        self.assertEqual(calls, [])

    def test_a_partial_merged_onto_a_superseded_baseline_is_dropped(self):
        """The loop can adopt between the baseline read and the delivery.

        That merged candidate is built on the older state, so delivering it
        would put the frozen value back - it must be dropped instead. The
        sanitizer hook runs exactly inside that window.
        """
        client, calls = _client()
        newer = {
            "code": 200,
            "instant": {"charge": {"battery_soc": 49}, "odometer": {"total_mileage": 62234}},
        }

        def sanitizer(instant, previous):
            client.adopt_state(copy.deepcopy(newer))
            return instant

        client.set_instant_sanitizer(sanitizer)
        client._on_message(
            None, None, _Msg(_state(instant={"door": {"driver_door_lock_st": 1}}))
        )
        self.assertEqual(calls, [])
        self.assertEqual(client._last_full_content, newer)


class AdoptionIsAReplacement(unittest.TestCase):
    def test_the_client_holds_the_adopted_state_not_a_blend(self):
        client, _calls = _client()
        adopted = _adopt(client, 49)
        self.assertEqual(client._last_full_content, adopted)
        self.assertEqual(
            client._last_full_content["instant"]["charge"]["battery_soc"], 49
        )

    def test_a_full_state_after_the_adoption_replaces_it(self):
        client, calls = _client()
        _adopt(client, 49)
        full = _state(
            instant={
                "charge": {"battery_soc": 48},
                "odometer": {"total_mileage": 62240},
                "drive": {"speed": 0, "shift_state": 4},
                "ext": {"vehicleMode": 1},
            }
        )
        client._on_message(None, None, _Msg(full))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[-1]["msg_content"]["instant"]["odometer"]["total_mileage"], 62240
        )


if __name__ == "__main__":
    unittest.main()
