#!/usr/bin/env python3
"""Synthetic regression frames for startup and successive partial telemetry."""
import unittest
from test_mqtt_state_delivery import _client, _Msg, _state

class InitialPartialTelemetry(unittest.TestCase):
    def fresh(self):
        client, calls = _client()
        client._last_full_content = None
        return client, calls

    def test_first_charge_without_ext_is_delivered(self):
        client, calls = self.fresh()
        client._on_message(None, None, _Msg(_state(instant={"charge": {"battery_soc": 74}})))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["msg_content"]["instant"]["charge"]["battery_soc"], 74)
        self.assertGreater(client.last_meaningful_state_ts, 0)

    def test_first_hvac_and_seat_sections_do_not_need_charge(self):
        for section, value in (("hvac", {"hvac_inner_temp_float": 21}),
                               ("seat", {"driver_seat_heat": 0})):
            with self.subTest(section=section):
                client, calls = self.fresh()
                client._on_message(None, None, _Msg(_state(instant={section: value})))
                self.assertEqual(len(calls), 1)

    def test_empty_ack_does_not_seed_or_stamp_a_baseline(self):
        for instant in ({}, {"charge": {}}, {"unrecognised": {"value": 1}}):
            with self.subTest(instant=instant):
                client, calls = self.fresh()
                before = client.last_meaningful_state_ts
                client._on_message(None, None, _Msg(_state(instant=instant)))
                self.assertEqual(calls, [])
                self.assertIsNone(client._last_full_content)
                self.assertEqual(client.last_meaningful_state_ts, before)

    def test_refused_initial_state_never_becomes_baseline(self):
        client, calls = self.fresh()
        client.set_state_callback(lambda data: (calls.append(data), False)[1])
        before = client.last_meaningful_state_ts
        client._on_message(None, None, _Msg(_state(instant={"charge": {"battery_soc": 74}})))
        self.assertEqual(len(calls), 1)
        self.assertIsNone(client._last_full_content)
        self.assertEqual(client.last_meaningful_state_ts, before)

class SuccessivePartialTelemetry(unittest.TestCase):
    def test_partial_keeps_other_fields_in_the_same_section(self):
        client, calls = _client({"code": 200, "instant": {"charge": {"battery_soc": 74, "max_range_charge": 80}}})
        client._on_message(None, None, _Msg(_state(instant={"charge": {"battery_soc": 73}})))
        self.assertEqual(calls[-1]["msg_content"]["instant"]["charge"],
                         {"battery_soc": 73, "max_range_charge": 80})

    def test_next_partial_keeps_the_previous_accepted_change(self):
        client, calls = _client()
        client._on_message(None, None, _Msg(_state(instant={"charge": {"battery_soc": 73}})))
        client._on_message(None, None, _Msg(_state(instant={"hvac": {"hvac_inner_temp_float": 21}})))
        self.assertEqual(calls[-1]["msg_content"]["instant"]["charge"]["battery_soc"], 73)

    def test_interval_updates_keep_other_fields_and_sections(self):
        client, calls = _client({"code": 200, "instant": {},
                                "interval": {"charge": {"power": 1000, "voltage": 230}, "drive": {"speed": 0}}})
        client._on_message(None, None, _Msg(_state(interval={"charge": {"power": 0}})))
        interval = calls[-1]["msg_content"]["interval"]
        self.assertEqual(interval["charge"], {"power": 0, "voltage": 230})
        self.assertEqual(interval["drive"], {"speed": 0})

    def test_identical_repeat_is_still_not_fresh_data(self):
        client, calls = _client()
        frame = _Msg(_state(instant={"charge": {"battery_soc": 73}}))
        client._on_message(None, None, frame)
        before = client.last_meaningful_state_ts
        client._on_message(None, None, frame)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.last_meaningful_state_ts, before)

if __name__ == "__main__":
    unittest.main()
