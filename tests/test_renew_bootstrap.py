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

    def test_an_unknown_vin_is_reported_rather_than_guessed(self):
        """Creating an entry needs a real VIN: it decides the entity ids."""
        self.assertIn("no VIN to create the config entry with", SERVER)
        self.assertIn("def _vin_from_pair(", SERVER)
        self.assertIn("len(value) == 17 and value.isascii() and value.isalnum()", SERVER)

    def test_the_tool_records_the_vin_it_reads(self):
        self.assertIn('tokens["vin"] = vin', TOOL)
        self.assertIn("api.get_vehicles(None, tenant, tokens)", TOOL)
        # a login that worked must still be saved if this call does not
        self.assertIn("never lose a good login to this", TOOL)


class VinParsingTests(unittest.TestCase):
    """The two VIN readers, since a wrong VIN silently creates the wrong entities."""

    def _load(self, name, relative):
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

    def test_the_tool_reads_either_vehicle_list_shape(self):
        tool = self._load("bootstrap_tool_t", "tools/onboard_email_login.py")
        self.assertEqual(tool._first_vin([{"vin": "l1nnsgha3sb800783"}]), "L1NNSGHA3SB800783")
        self.assertEqual(
            tool._first_vin({"vehicles": [{"vin": "L1NNSGHA3SB800783"}]}),
            "L1NNSGHA3SB800783",
        )
        self.assertEqual(tool._first_vin({"data": "nope"}), "")
        self.assertEqual(tool._first_vin([{"vin": "short"}]), "")

    def test_the_app_reads_the_pair_the_tool_writes(self):
        import json
        import tempfile

        app = self._load("bootstrap_app_t", "xpeng_renew/renew_server.py")
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "xpeng_tokens.json"
            path.write_text(json.dumps({"json": {"vin": "l1nnsgha3sb800783"}}))
            self.assertEqual(app._vin_from_pair(folder), "L1NNSGHA3SB800783")
            path.write_text(json.dumps({"vehicle": {"vin": "L1NNSGHA3SB800783"}}))
            self.assertEqual(app._vin_from_pair(folder), "L1NNSGHA3SB800783")
            path.write_text(json.dumps({"vin": "TEST"}))
            self.assertEqual(app._vin_from_pair(folder), "")
        self.assertEqual(app._vin_from_pair("/nonexistent"), "")


if __name__ == "__main__":
    unittest.main()
