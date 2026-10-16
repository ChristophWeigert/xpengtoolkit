#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A pushed entity must not be able to ask its coordinator to refresh.

    python3 tests/test_entity_update_guard.py

``CoordinatorEntity.async_update`` requests a coordinator refresh. This
coordinator is push-only and defines no ``_async_update_data``, so the request
fails, ``last_update_success`` flips to False, and ``CoordinatorEntity.available``
— which returns exactly that flag — marks *every* entity in the integration
unavailable until the next push arrives.

One ``homeassistant.update_entity`` call is enough to trigger it: from a dashboard
button, a script, or a well-meaning "refresh the car now" automation. On a parked
car the next push can be hours away, so the whole integration reads as flapping —
which is why the guard belongs on the base entity rather than only on the two
classes that poll themselves.

Checked statically: ``entity.py`` imports Home Assistant, which is not installed
here (the same reason ``test_poll_status`` loads its module by path).
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

ENTITY = (_component.component_dir() / "entity.py").read_text(encoding="utf-8")
SENSOR = (_component.component_dir() / "sensor.py").read_text(encoding="utf-8")


def _method(source: str, class_name: str, method: str) -> ast.AST | None:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == method
            ):
                return child
    return None


def _body(source: str) -> list[ast.stmt]:
    """``XPengEntity.async_update``'s statements, minus its docstring."""
    node = _method(source, "XPengEntity", "async_update")
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    return body


class BaseEntityUpdateTests(unittest.TestCase):
    def test_the_base_entity_overrides_async_update(self):
        self.assertIsNotNone(
            _method(ENTITY, "XPengEntity", "async_update"),
            "XPengEntity must override async_update — see the method's docstring",
        )

    def test_the_override_requests_nothing(self):
        dump = ast.dump(ast.Module(body=_body(ENTITY), type_ignores=[]))
        self.assertNotIn("async_request_refresh", dump)
        self.assertNotIn("async_refresh", dump)
        self.assertNotIn("coordinator", dump)

    def test_the_override_is_still_a_no_op(self):
        """Work smuggled into this hook runs on every update request."""
        self.assertEqual(
            _body(ENTITY), [], "async_update is a no-op; keep it empty"
        )


class PolledClassesKeepPollingTests(unittest.TestCase):
    """The no-op must not have taken `should_poll` with it."""

    def test_the_polled_sensor_still_declares_should_poll(self):
        self.assertIsNotNone(_method(SENSOR, "XPengPolledSensor", "should_poll"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
