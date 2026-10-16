#!/usr/bin/env python3
"""Three changes from the 2026-10-17 charging session, pinned.

That session produced the evidence the three of them rest on:

* parked and asleep, the car answers every query with a stub, so the watchdog's
  ten-minute reconnect bought nothing while costing 59 reconnects and most of the
  50 ``Not authorized`` refusals in a day;
* once the session started, a *passive* query returned full states immediately -
  the car was awake, and the push stream had simply never started;
* the refusal line named no cause, and the credential's age is the one number
  that separates "our rotation superseded it" from "the broker refused a fresh
  one".

The fourth change - an external charger saying a session has started - is here for
the same reason: the charger knew about 7.4 kW for fifteen minutes before the car
mentioned anything.

Run from the repo root:

    python3 tests/test_quiet_when_parked.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

INIT = (_component.component_dir() / "__init__.py").read_text(encoding="utf-8")
CLIENT = (_component.component_dir() / "mqtt_client.py").read_text(encoding="utf-8")


class TestTheParkedReconnectIsRare(unittest.TestCase):
    def test_there_is_a_longer_backoff_for_a_parked_car(self):
        self.assertIn("PARKED_RECONNECT_BACKOFF = 1800", INIT)
        self.assertIn("RECONNECT_BACKOFF = 600", INIT)

    def test_the_watchdog_chooses_between_them(self):
        tree = ast.parse(INIT)
        watchdog = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_silence_watchdog"
        )
        body = ast.unparse(watchdog)
        self.assertIn("PARKED_RECONNECT_BACKOFF if parked else RECONNECT_BACKOFF", body)
        # Both reconnect paths have to use the choice, or one of them still hammers.
        self.assertEqual(body.count("reconnect_backoff"), 3, body)


class TestTheHintNeverWakes(unittest.TestCase):
    """The cadence may rise on someone else's word; the wake may not."""

    def test_the_force_upload_still_depends_on_the_car_alone(self):
        tree = ast.parse(INIT)
        query = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_periodic_query"
        )
        body = ast.unparse(query)
        # `active` gates the wake, the hint only gates how soon we ask.
        self.assertIn("active = coordinator.is_charging or coordinator.vehicle_parked is False", body)
        self.assertIn("if active:", body)
        self.assertNotIn("if active or hint:", body)
        self.assertNotIn("hint or active", body)

    def test_the_interval_helper_takes_the_hint(self):
        source = (_component.component_dir() / "poll_status.py").read_text(encoding="utf-8")
        self.assertIn("charging_hint: bool = False", source)
        self.assertIn("if is_active(parked, is_charging) or charging_hint:", source)


class TestTheRefusalSaysWhy(unittest.TestCase):
    def test_the_credential_clock_is_set_where_the_password_is(self):
        # Constructor and stage_credentials, i.e. everywhere the password changes.
        self.assertEqual(CLIENT.count("self._password_set_at = time.time()"), 2)

    def test_the_refusal_reports_its_age_and_reads_it(self):
        self.assertIn("credential ", CLIENT)
        self.assertIn("staged %.0fs ago", CLIENT)
        self.assertIn("superseded by our own rotation", CLIENT)
        self.assertIn("current, so the broker refused a fresh credential", CLIENT)
        # The line only means something if the age it prints is in the same message.
        refusal = CLIENT[CLIENT.index("XPENG MQTT connection refused") - 900 :]
        self.assertIn("age", refusal[:1200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
