#!/usr/bin/env python3
"""The 7-day energy-report sensor is gone, and stays gone.

Removed in 0.6.6. XPENG gates the consumption figure per account: on a shared
(secondary) account the endpoint answers 0.0 with no error at all while the owner's
app shows a real number - verified 2026-10-09, app "16.9 kWh/100km" against an API
0.0. On such an account the entity is a permanent zero that can only be explained,
never fixed, so it was removed rather than left to mislead.

The removal has two halves and both are checked here, because either one alone
leaves a mess: the platform must stop creating the entity, and the registry entry
the old version created must be deleted, or an upgraded install keeps a ghost entity
that is unavailable forever.

Run from the repo root:

    python3 tests/test_energy_report_removed.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENSOR_SOURCE = (_component.component_dir() / "sensor.py").read_text(encoding="utf-8")


class TestTheEntityIsNoLongerCreated(unittest.TestCase):
    def test_the_class_is_gone(self):
        self.assertNotIn("XPengEnergyReportSensor", SENSOR_SOURCE)

    def test_the_translation_is_gone(self):
        for name in ("strings.json", "translations/en.json"):
            path = _component.component_dir() / name
            self.assertNotIn('"energy_report"', path.read_text(encoding="utf-8"), name)


class TestTheOldRegistryEntryIsRemoved(unittest.TestCase):
    """Otherwise the upgrade leaves an unavailable ghost entity behind."""

    def test_the_old_unique_id_is_listed(self):
        self.assertIn('"xpeng_{vin}_energy_report"', SENSOR_SOURCE)

    def test_the_prune_runs_during_setup(self):
        tree = ast.parse(SENSOR_SOURCE)
        setup = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
        )
        body = ast.unparse(setup)
        self.assertIn("_prune_removed_entities", body)
        # ...and it removes from the registry rather than only logging.
        prune = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_prune_removed_entities"
        )
        self.assertIn("async_remove", ast.unparse(prune))


if __name__ == "__main__":
    unittest.main(verbosity=2)
