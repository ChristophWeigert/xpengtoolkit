"""Keep lightweight HA/component stubs local to each unittest module.

Discovery imports all test modules before running their cases. Restore import
side effects immediately, then reinstall that module's fixtures only while its
tests execute. Real standard-library and installed dependency imports are kept.
"""
from __future__ import annotations

import calendar  # Preload before a component calendar.py can shadow stdlib.
from pathlib import Path
import select  # Likewise for the select platform.
import sys

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"


def _fixture(name, module) -> bool:
    if name.startswith(("homeassistant", "custom_components")):
        return True
    filename = getattr(module, "__file__", None)
    if filename is None:
        return getattr(module, "__spec__", None) is None
    try:
        return Path(filename).resolve().is_relative_to(COMPONENT)
    except (TypeError, ValueError):
        return False


class ImportIsolation:
    def __init__(self):
        self.before = dict(sys.modules)
        self.path_before = list(sys.path)
        self.modules = {}
        self.path = []

    def use(self, dependency):
        """Activate a cached fixture module before extending its HA stubs."""
        fixture = getattr(dependency, "_IMPORTS", None)
        if fixture:
            fixture.activate()

    def activate(self):
        sys.modules.update(self.modules)
        sys.path[:] = self.path

    def restore(self):
        for name, module in list(sys.modules.items()):
            if self.before.get(name) is not module and _fixture(name, module):
                if name in self.before:
                    sys.modules[name] = self.before[name]
                else:
                    sys.modules.pop(name, None)
        sys.path[:] = self.path_before

    def finish(self, namespace):
        self.modules = {
            name: module for name, module in sys.modules.items()
            if self.before.get(name) is not module and _fixture(name, module)
        }
        self.path = list(sys.path)
        self.restore()
        old_setup = namespace.get("setUpModule")
        old_teardown = namespace.get("tearDownModule")
        execution = []

        def setup():
            execution.append(ImportIsolation())
            self.activate()
            try:
                if old_setup:
                    old_setup()
            except BaseException:
                execution.pop().restore()
                raise

        def teardown():
            try:
                if old_teardown:
                    old_teardown()
            finally:
                execution.pop().restore()

        namespace["setUpModule"] = setup
        namespace["tearDownModule"] = teardown
