"""An energy report of 0.0 must not be presented as an unambiguous zero.

XPENG gates consumption data per account. Measured 2026-10-09: the owner's app
showed "16.9 kWh/100km - 7-day average energy consumption" while
`/flow/cv2/app/vehicle/energy/simpleReport` returned exactly
`avgTripEnergyPowerOf7Days: 0.0` for a secondary account - with no error at all.

So a bare 0.0 is a lie by omission: it reads as "this car used no energy", when
it can equally mean "this account is not allowed to see the number". The sensor
now says which, in an attribute, so an automation or a human is not misled.
"""
import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _component  # noqa: E402


def _load_attr_fn(class_name: str, name: str):
    """Pull one property getter out of a named class in sensor.py.

    sensor.py imports Home Assistant at module scope and the entity classes
    subclass from it, so neither the module nor the class is importable here.
    Extract just the FunctionDef, but select it by its owning class name -
    several sensors define `extra_state_attributes`, and taking the first one
    silently tests the wrong entity.
    """
    src = (_component.component_dir() / "sensor.py").read_text()
    tree = ast.parse(src)
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                mod = ast.Module(body=[node], type_ignores=[])
                ns: dict = {"Any": object, "dict": dict, "float": float,
                            "TypeError": TypeError, "ValueError": ValueError,
                            "property": property, "object": object}
                exec(compile(mod, "<attr>", "exec"), ns)  # noqa: S102
                fn = ns[name]
                return fn.fget if isinstance(fn, property) else fn
    return None


class TestEnergyZeroIsExplained(unittest.TestCase):
    def setUp(self):
        self.fn = _load_attr_fn("XPengEnergyReportSensor", "extra_state_attributes")
        self.assertIsNotNone(self.fn, "extra_state_attributes not found")

        class _Coord:
            def __init__(self, report):
                self.energy_report = report

        class _Self:
            pass

        self._Coord = _Coord
        self._Self = _Self

    def _attrs(self, report):
        obj = self._Self()
        obj.coordinator = self._Coord(report)
        return self.fn(obj)

    def test_zero_is_flagged_as_possibly_restricted(self):
        attrs = self._attrs({"avgTripEnergyPowerOf7Days": 0.0})
        self.assertIn("zero_may_mean_restricted", attrs)
        self.assertIn("per account", attrs["zero_may_mean_restricted"])

    def test_a_real_reading_is_not_flagged(self):
        attrs = self._attrs({"avgTripEnergyPowerOf7Days": 16.9})
        self.assertNotIn("zero_may_mean_restricted", attrs)
        self.assertEqual(attrs["avg_7day_kwh_per_100km"], 16.9)

    def test_missing_average_is_not_flagged(self):
        # No value at all is a different condition from a zero value.
        attrs = self._attrs({"reportUrl": "https://example.invalid/x"})
        self.assertNotIn("zero_may_mean_restricted", attrs)

    def test_rejects_a_non_numeric_average_without_raising(self):
        attrs = self._attrs({"avgTripEnergyPowerOf7Days": "n/a"})
        self.assertNotIn("zero_may_mean_restricted", attrs)

    def test_no_report_returns_empty(self):
        self.assertEqual(self._attrs(None), {})

    def test_zero_via_string_is_still_flagged(self):
        # The API has returned numbers as strings elsewhere; treat "0.0" the same.
        attrs = self._attrs({"avgTripEnergyPowerOf7Days": "0.0"})
        self.assertIn("zero_may_mean_restricted", attrs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
