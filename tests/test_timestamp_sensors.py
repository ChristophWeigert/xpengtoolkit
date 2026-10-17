#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Timestamp entities must expose a datetime, not a formatted string.

Reported by a user (2026-10-17): `sensor.xpeng_<id>_last_data_update` showed
"18 Oct 2026 12:06:56 AM" — a string in whatever wording the writer chose, which
Home Assistant cannot localise, cannot age, and which renders wrong on a
dashboard. The fix is `device_class: timestamp` plus a `datetime` (or None) native
value; Home Assistant then formats it in the reader's timezone/language.

Also covers `ota_last_checked`, which had the same formatted-string shape.

Structural, like the rest of the suite's wiring checks: importing the modules
needs Home Assistant, so this asserts on the source (and on the pure parser).
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "xpeng"


def _source(name: str) -> str:
    return (COMPONENT / name).read_text(encoding="utf-8")


class LastDataUpdateTimestampTests(unittest.TestCase):
    def test_the_old_entity_is_a_timestamp_sensor(self):
        source = _source("sensor.py")
        start = source.index("class XPengLastUpdateSensor(")
        block = source[start:source.index("class XPengLastDataUpdateSensor(", start)]
        self.assertIn("_attr_device_class = SensorDeviceClass.TIMESTAMP", block)
        self.assertIn("def native_value(self) -> datetime | None", block)

    def test_it_returns_the_datetime_not_a_formatting_of_it(self):
        """The state must be the datetime; formatting is Home Assistant's job."""
        source = _source("sensor.py")
        start = source.index("class XPengLastUpdateSensor(")
        block = source[start:source.index("class XPengLastDataUpdateSensor(", start)]
        self.assertNotIn("strftime", block)
        self.assertNotIn('"No data"', block)

    def test_no_sentinel_string_for_an_unknown_timestamp(self):
        """A timestamp sensor with no timestamp reports `unknown` (None)."""
        source = _source("sensor.py")
        start = source.index("class XPengLastUpdateSensor(")
        block = source[start:source.index("class XPengLastDataUpdateSensor(", start)]
        self.assertIn("getattr(self.coordinator, \"last_data_timestamp\", None)", block)


class OtaTimestampTests(unittest.TestCase):
    def test_the_ota_entity_is_a_timestamp_sensor(self):
        source = _source("const.py")
        entry = source[source.index('key="ota_last_checked"'):]
        entry = entry[:entry.index("),")]
        self.assertIn("device_class=SensorDeviceClass.TIMESTAMP", entry)

    def test_the_parser_returns_a_datetime(self):
        source = _source("const.py")
        start = source.index("def _parse_ota_timestamp(")
        block = source[start:source.index("\n# Config keys", start)]
        self.assertNotIn("strftime", block)
        self.assertIn("return _dt.fromtimestamp(ts / 1000, tz=_tz.utc)", block)


class NoFormattedTimestampStatesTests(unittest.TestCase):
    """No sensor should keep pre-formatting a timestamp into its state."""

    def test_sensor_py_has_no_strftime_in_a_native_value(self):
        tree = ast.parse(_source("sensor.py"))

        offenders: list[str] = []

        class Finder(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # noqa: N802
                if node.name == "native_value":
                    for child in ast.walk(node):
                        if isinstance(child, ast.Attribute) and child.attr == "strftime":
                            offenders.append(
                                f"line {getattr(child, 'lineno', '?')}"
                            )
                self.generic_visit(node)

        Finder().visit(tree)
        self.assertEqual(offenders, [], f"native_value still formats: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
