#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A fresh install must be able to finish its first renewal with no human.

The first run used to stop dead: the app minted a pair, called
`xpeng.import_credentials`, and got

    import_credentials returned 400: No XPENG config entry to import credentials into

because a renewal *imports* the pair and importing needs somewhere to import it to.
Completing the install was therefore a manual step, documented as one.

It cannot be fixed by teaching that service to create the entry, which is worth
recording because it is the obvious fix. Measured on a real install (2026-10-11)
with no config entry:

  * the `xpeng` service domain is ABSENT from /api/services - the integration's
    component-level setup only runs once an entry exists, so there is no
    `import_credentials` to call;
  * starting a config flow does not load it either.

What *is* loaded on demand is the config flow, so the app drives it: start the flow,
give the VIN, hand it the pair paths. Creating the entry is what loads the component
and registers the service for every renewal after it - verified end to end, service
REGISTERED immediately afterwards.

The one piece of identity the pair cannot supply was the VIN, which the flow
requires. The onboarding tool now records it from the vehicle list, so nothing has
to be typed; the app's `vin` option exists for pairs written before that.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = (ROOT / "xpeng_renew" / "renew_server.py").read_text(encoding="utf-8")
TOOL = (ROOT / "tools" / "onboard_email_login.py").read_text(encoding="utf-8")


def _load_module(name: str, relative: str):
    """Import a file from the repo, skipping the test if its deps are missing."""
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        raise unittest.SkipTest(f"{relative} could not be imported: {exc}")
    return module


class BootstrapTests(unittest.TestCase):
    def test_the_app_bootstraps_through_the_config_flow(self):
        """Not through the service: with no entry there is no service to call."""
        self.assertIn("def _bootstrap_entry(", SERVER)
        self.assertIn('"/config/config_entries/flow"', SERVER)
        self.assertIn('"auth_method": "import"', SERVER)
        self.assertIn("_existing_entry()", SERVER)

    def test_a_bootstrap_is_not_followed_by_an_import(self):
        """The flow already stored the pair; importing again would be redundant.

        It would also fail on the very install this exists for, because the service
        it would call has only just been registered by creating the entry.
        """
        marker = "if _existing_entry() is None:"
        self.assertIn(marker, SERVER)
        block = SERVER[SERVER.index(marker):]
        self.assertIn('"bootstrapped"', block[:1200])

    def test_the_bootstrap_asks_the_flow_which_step_it_landed_on(self):
        """The flow decides the step; submitting the wrong step's fields is a 400.

        Measured on the first fresh install against 0.4.16: the flow skipped straight to
        `import_tokens` because a pair was already in `pair_dir`, and the app's `vin` +
        `auth_method` payload was refused by that step's schema with

            "not a valid option at 'auth_method'"

        so the pair was minted and the entry was never created. 0.1.14 did exactly that.
        """
        start = SERVER.index("def _bootstrap_entry(")
        block = SERVER[start:SERVER.index("def _notify(", start)]
        # the auth-method payload is guarded by a check on the step that came back
        guard = block.index('if body.get("step_id") == "user":')
        payload = block.index('"auth_method": "import"')
        self.assertLess(guard, payload, "auth_method must not be sent unconditionally")
        self.assertIn('if body.get("step_id") != "import_tokens":', block)
        self.assertIn("not a valid option at 'auth_method'", block)

    def test_an_unknown_vin_is_reported_rather_than_guessed(self):
        """Creating an entry needs a real VIN: it decides the entity ids."""
        self.assertIn("no VIN to create the config entry with", SERVER)
        self.assertIn("def _vin_from_pair(", SERVER)
        self.assertIn("len(value) == 17 and value.isascii() and value.isalnum()", SERVER)

    def test_a_disabled_entry_does_not_count_as_an_install(self):
        """It suppresses the bootstrap and looks like a working install.

        Measured 2026-10-13: the entry was disabled (so absent from the HA UI), and
        `_no_entry_yet()` still found it, skipped the fresh-install renewal and left
        the app waiting out a 6000s interval against a pair it had never minted. The
        entry also held the VIN as its unique_id, so the flow would have answered
        `already_configured` - the bootstrap would have failed even when it did run.
        """
        start = SERVER.index("def _no_entry_yet(")
        block = SERVER[start:SERVER.index("def _credentials_entity(", start)]
        self.assertIn('entry.get("disabled_by")', block)
        # the domain check must still be there, or every install looks bootstrapped
        self.assertIn('entry.get("domain") == "xpeng"', block)
        # ...and "could not ask" must stay False (it must not start a renewal)
        self.assertIn("if status != 200 or not isinstance(body, list):", block)

    def test_the_tool_records_the_vin_it_reads(self):
        self.assertIn('tokens["vin"] = vin', TOOL)
        self.assertIn("api.get_vehicles(None, tenant, tokens)", TOOL)
        # a login that worked must still be saved if this call does not
        self.assertIn("never lose a good login to this", TOOL)


class VinParsingTests(unittest.TestCase):
    """The two VIN readers, since a wrong VIN silently creates the wrong entities."""

    def test_the_tool_reads_either_vehicle_list_shape(self):
        tool = _load_module("bootstrap_tool_t", "tools/onboard_email_login.py")
        self.assertEqual(tool._first_vin([{"vin": "l1nnsgha3sb123456"}]), "L1NNSGHA3SB123456")
        self.assertEqual(
            tool._first_vin({"vehicles": [{"vin": "L1NNSGHA3SB123456"}]}),
            "L1NNSGHA3SB123456",
        )
        self.assertEqual(tool._first_vin({"data": "nope"}), "")
        self.assertEqual(tool._first_vin([{"vin": "short"}]), "")

    def test_the_app_reads_the_pair_the_tool_writes(self):
        import json
        import tempfile

        app = _load_module("bootstrap_app_t", "xpeng_renew/renew_server.py")
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "xpeng_tokens.json"
            path.write_text(json.dumps({"json": {"vin": "l1nnsgha3sb123456"}}))
            self.assertEqual(app._vin_from_pair(folder), "L1NNSGHA3SB123456")
            path.write_text(json.dumps({"vehicle": {"vin": "L1NNSGHA3SB123456"}}))
            self.assertEqual(app._vin_from_pair(folder), "L1NNSGHA3SB123456")
            path.write_text(json.dumps({"vin": "TEST"}))
            self.assertEqual(app._vin_from_pair(folder), "")
        self.assertEqual(app._vin_from_pair("/nonexistent"), "")


if __name__ == "__main__":
    unittest.main()

class SchedulerTests(unittest.TestCase):
    """The app is its own clock now, so the policy has to be right on its own.

    Step 7 of the README used to be three YAML blocks in configuration.yaml, because
    something had to call the app. It calls itself instead - and the one thing the
    Home Assistant automation did that a plain timer cannot is react to credentials
    that have already gone, so that override is part of the policy and is tested.
    """

    def _app(self):
        return _load_module("scheduler_app_t", "xpeng_renew/renew_server.py")

    def test_the_interval_decides_when_nothing_is_wrong(self):
        app = self._app()
        self.assertEqual(app._renewal_due(now=100, next_run=100, credentials_invalid=False), "due")
        self.assertEqual(app._renewal_due(now=99, next_run=100, credentials_invalid=False), "")

    def test_invalid_credentials_do_not_wait_for_the_interval(self):
        app = self._app()
        self.assertEqual(
            app._renewal_due(now=1, next_run=10_000, credentials_invalid=True),
            "credentials already invalid",
        )

    def test_being_due_wins_over_the_reason(self):
        """Both true is the ordinary case at the two-hour mark."""
        app = self._app()
        self.assertEqual(app._renewal_due(now=10_000, next_run=10_000, credentials_invalid=True), "due")

    def test_the_scheduler_can_be_turned_off(self):
        """An install that would rather drive renewals from HA keeps that option."""
        options = (ROOT / "xpeng_renew" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("renew_interval", options)
        self.assertIn("renew_interval: int?", options)
        self.assertIn("RENEW_INTERVAL <= 0", SERVER)
        self.assertIn("scheduler disabled", SERVER)

    def test_the_schedule_is_visible(self):
        """The HA helper used to show when a renewal last ran; /healthz replaces it."""
        self.assertIn('"scheduler"', SERVER)
        self.assertIn("next_run_s", SERVER)

    def test_the_app_starts_its_own_scheduler(self):
        self.assertIn('threading.Thread(target=_scheduler', SERVER)
        self.assertIn("daemon=True", SERVER)

    def test_the_credentials_probe_reads_the_integration_sensor(self):
        """Without this the fast path is gone and a dead session waits out the hour."""
        self.assertIn("_credentials_valid", SERVER)
        self.assertIn("binary_sensor.xpeng_", SERVER)

    def test_a_fresh_install_bootstraps_at_startup(self):
        """Waiting out a whole interval is what made "trigger it once" a step."""
        app = self._app()
        app._core_json = lambda path, payload=None, method="POST": (200, [{"domain": "hassio"}])
        self.assertTrue(app._no_entry_yet())
        self.assertIn("no config entry yet: renewing now to create it", SERVER)

    def test_an_unreadable_api_does_not_look_like_a_fresh_install(self):
        """_existing_entry() cannot tell "no entry" from "could not ask"; this must.

        Otherwise an API that is briefly down at startup would set off a renewal.
        """
        app = self._app()
        for status, body in ((502, "bad gateway"), (200, {"nope": True}), (0, "boom")):
            app._core_json = (
                lambda s, b: (lambda path, payload=None, method="POST": (s, b))
            )(status, body)
            self.assertFalse(app._no_entry_yet(), (status, body))

    def test_a_working_install_is_left_alone_at_startup(self):
        app = self._app()
        app._core_json = lambda path, payload=None, method="POST": (200, [{"domain": "xpeng"}])
        self.assertFalse(app._no_entry_yet())
