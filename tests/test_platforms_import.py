#!/usr/bin/env python3
"""The platform modules must at least import, and define things before using them.

0.6.8 shipped ``binary_sensor.py`` with ``XPengPluggedInBinarySensor`` declared above
the ``_PolledHealthBinarySensor`` mix-in it inherits from. That is a ``NameError``
during import, so **every** install failed to set up - and nothing here noticed,
because no test imported a platform module at all. ``py_compile`` does not catch it
either: the file is syntactically perfect.

So there are two checks here, and the first is the one that matters:

1. each platform file is *imported*, with Home Assistant and the package's own
   dependencies stubbed, which turns any import-time error into a test failure;
2. every module-level class is defined before anything that names it in a base list,
   which names the ordering mistake directly instead of leaving it as a traceback.

They are smoke tests, not behaviour tests. Importing a file proves nothing about what
it does - it proves the file loads, which is the difference between an upgrade and an
outage.

Run from the repo root:

    python3 tests/test_platforms_import.py
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

#: The platforms this covers. sensor.py and binary_sensor.py are the two that have
#: grown custom entity classes (the things that can be defined out of order); add
#: more here as they do.
PLATFORMS = ("binary_sensor.py", "sensor.py")


class _Anything:
    """Stand-in for the constants and enums a platform imports at module level."""

    def __getattr__(self, name):
        return name


class _AutoModule(types.ModuleType):
    """A stub module: any name asked of it exists (PEP 562)."""

    def __getattr__(self, name):  # noqa: D105 - trivial
        return _Anything()


def _entity_base(name: str) -> type:
    """A distinct empty class: two ``object`` bases in one class body is a TypeError."""
    return type(name, (), {})


def _stub_ha() -> None:
    def mod(name: str, **attrs) -> None:
        module = _AutoModule(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    mod("homeassistant")
    mod("homeassistant.components")
    mod(
        "homeassistant.components.binary_sensor",
        BinarySensorDeviceClass=_Anything(),
        BinarySensorEntity=_entity_base("BinarySensorEntity"),
    )
    mod(
        "homeassistant.components.sensor",
        SensorDeviceClass=_Anything(),
        SensorEntity=_entity_base("SensorEntity"),
        SensorStateClass=_Anything(),
    )
    mod("homeassistant.config_entries", ConfigEntry=_Anything())
    # Real constants class, so EntityCategory/UnitOfTemperature/UnitOfTime resolve.
    mod("homeassistant.const")
    mod("homeassistant.core", HomeAssistant=_Anything(), callback=lambda fn: fn)
    mod("homeassistant.helpers")
    mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=_Anything())
    mod("homeassistant.helpers.entity_registry", async_get=lambda hass: _Anything())
    mod("homeassistant.helpers.event")
    mod("homeassistant.exceptions", HomeAssistantError=Exception)


def _stub_package(component: pathlib.Path) -> None:
    """Fabricate the package so the platform's relative imports resolve."""
    package = "custom_components.xpeng"
    sys.modules.setdefault("custom_components", _AutoModule("custom_components"))
    pkg = _AutoModule(package)
    pkg.__path__ = [str(component)]  # type: ignore[attr-defined]
    sys.modules[package] = pkg

    for name, attrs in {
        "const": {
            "DOMAIN": "xpeng",
            "BINARY_SENSOR_DESCRIPTIONS": (),
            "SENSOR_DESCRIPTIONS": (),
            "CONF_CLIENT_ID": "client_id",
            "CONF_MQTT_REFRESH_INTERVAL": "mqtt_refresh_interval",
            "DEFAULT_MQTT_REFRESH_INTERVAL": 45,
        },
        "coordinator": {"XPengDataCoordinator": _Anything()},
        "entity": {"XPengEntity": _entity_base("XPengEntity")},
        "health": {},
        "poll_status": {},
        "vin_descriptor": {"mask_vin": lambda vin: vin},
        "capture": {},
        "can_journal": {},
    }.items():
        module = _AutoModule(f"{package}.{name}")
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[f"{package}.{name}"] = module

    # charge_port is pure, so it is imported for real: a broken platform import of it
    # must fail here, not be papered over by a stub.
    path = component / "charge_port.py"
    spec = importlib.util.spec_from_file_location(f"{package}.charge_port", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package}.charge_port"] = module
    spec.loader.exec_module(module)


class TestPlatformsImport(unittest.TestCase):
    #: sys.modules as it was before stubbing. The stubs must not outlive this test:
    #: other tests in the same discovery run load the real modules by name, and a
    #: fake ``custom_components.xpeng.const`` left behind makes them test the stub
    #: instead (that happened: test_reauth_repair started failing when this file
    #: ran first).
    _saved_modules: dict[str, object] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.component = _component.component_dir()
        cls._saved_modules = dict(sys.modules)
        _stub_ha()
        _stub_package(cls.component)

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name not in cls._saved_modules:
                del sys.modules[name]
        sys.modules.update(cls._saved_modules)

    def test_each_platform_imports(self):
        for filename in PLATFORMS:
            with self.subTest(platform=filename):
                path = self.component / filename
                # Under the package, so the platform's relative imports resolve
                # to the stubs above instead of a nameless parent.
                name = f"custom_components.xpeng.{filename[:-3]}"
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                try:
                    spec.loader.exec_module(module)
                except NameError as err:
                    self.fail(
                        f"{filename} does not import: {err}. Something is used before "
                        "it is defined - the integration would fail to set up."
                    )


class TestDefinitionsComeFirst(unittest.TestCase):
    """A NameError in a base list is a startup failure, so check the order."""

    def test_no_class_is_declared_before_its_base(self):
        for filename in PLATFORMS:
            source = (_component.component_dir() / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            defined: set[str] = set()
            for node in tree.body:  # module level, in source order
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        if isinstance(base, ast.Name) and base.id in {
                            n.name for n in tree.body if isinstance(n, ast.ClassDef)
                        }:
                            with self.subTest(platform=filename, cls=node.name, base=base.id):
                                self.assertIn(
                                    base.id,
                                    defined,
                                    f"{filename}: {node.name} inherits {base.id}, which is "
                                    "defined later in the file - a NameError at import",
                                )
                    defined.add(node.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
