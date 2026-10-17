#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the credential and MQTT health entities. Standard library only.

    python3 tests/test_health_sensors.py
    python3 -m unittest discover -s tests -v

Why these exist: when a credential dies this integration goes quiet rather than
loud. The push stream stops, every vehicle entity freezes at its last value, and
that is indistinguishable from a parked car — "nothing arrived" is the *normal*
state of an asleep vehicle. The three entities added alongside this test are
supposed to make the broken case visible, so what is pinned here is

* the rule they decide with, ``health.credentials_health`` — deliberately free of
  Home Assistant so it can be exercised in a bare interpreter, and called by the
  entities rather than reimplemented, so these tests describe what ships; and
* the wiring that fails *silently* when it is wrong: a translation key with no
  name in ``translations/en.json`` leaves an entity unnamed, and a name another
  entity already claims lands the new one on a ``_2`` entity id instead of the
  id anyone would write in an automation.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

COMPONENT = _component.component_dir()

# Loaded by path, not imported as `custom_components.xpeng.health`: that would
# execute the package's __init__, which imports Home Assistant, which is not
# installed here (and must not need to be). This module is deliberately free of
# HA imports — that is the point of it existing.
_HEALTH_PATH = _component.module_path("health.py")
_spec = importlib.util.spec_from_file_location("health", _HEALTH_PATH)
assert _spec and _spec.loader
health = importlib.util.module_from_spec(_spec)
sys.modules["health"] = health
_spec.loader.exec_module(health)

#: The shipped default refresh interval, from const.py's
#: DEFAULT_MQTT_REFRESH_INTERVAL. Restated here rather than imported because
#: const.py imports Home Assistant; `test_the_shipped_interval_is_consts_default`
#: re-derives it from the source text so the two cannot drift apart.
DEFAULT_INTERVAL = 45

NOW = 1_000_000.0


def _health(**overrides):
    """`credentials_health` with the uninteresting arguments filled in.

    `interval` is accepted as a shorter spelling of `refresh_interval`, because
    the tests read better that way and the mapping is one line.
    """
    if "interval" in overrides:
        overrides["refresh_interval"] = overrides.pop("interval")
    params = {
        "now": NOW,
        "last_success": NOW - 1.0,
        "consecutive_failures": 0,
        "refresh_interval": DEFAULT_INTERVAL,
    }
    params.update(overrides)
    return health.credentials_health(**params)


# ── source-text helpers, for the wiring checks ──────────────────────────────


def _source(filename: str) -> str:
    return (COMPONENT / filename).read_text(encoding="utf-8")


def _tree(filename: str) -> ast.Module:
    return ast.parse(_source(filename), filename=str(COMPONENT / filename))


def _class_attr(filename: str, class_name: str, attr: str):
    """The value assigned to `attr` on `class_name`.

    Literals come back as values; anything else (such as
    ``SensorDeviceClass.TIMESTAMP``, which is not a literal) comes back as source
    text, because the point of the check is to pin *which* constant was named.
    """
    for node in ast.walk(_tree(filename)):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for stmt in node.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                targets = [stmt.target]
            for target in targets:
                if target.id == attr:
                    try:
                        return ast.literal_eval(stmt.value)
                    except (ValueError, SyntaxError):
                        return ast.unparse(stmt.value)
    return None


def _names_used_in(filename: str, func_name: str) -> set:
    """Every bare name referenced inside a module-level function."""
    used = set()
    for node in _tree(filename).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == func_name
        ):
            used.update(
                sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)
            )
    return used


def _imports_homeassistant(path: pathlib.Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "homeassistant" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "homeassistant":
                return True
    return False


def _const_int(name: str) -> int:
    """Read an int constant out of const.py without importing it."""
    for node in _tree("const.py").body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return ast.literal_eval(node.value)
    raise AssertionError(f"const.py defines no {name}")


def _entity_names(domain: str, filename: str = "translations/en.json") -> dict:
    data = json.loads((COMPONENT / filename).read_text(encoding="utf-8"))
    return {
        key: (entry or {}).get("name")
        for key, entry in data["entity"][domain].items()
    }


def _slug(name: str) -> str:
    """Home Assistant's object-id slug, near enough for ASCII entity names."""
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")


class TestCredentialsHealth(unittest.TestCase):
    """The decision itself — the case that matters is the false negative."""

    def test_a_fresh_success_with_no_failures_is_valid(self):
        result = _health(now=NOW, last_success=NOW - 5.0)
        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["age_s"], 5)
        self.assertEqual(result["stale_after_s"], 3 * DEFAULT_INTERVAL)

    def test_the_bound_is_inclusive(self):
        """`age_s <= stale_after_s`: exactly at the bound is still healthy.

        Off-by-one here would flap the entity at the precise moment it is
        supposed to be reporting a healthy loop.
        """
        result = _health(now=NOW, last_success=NOW - 3 * DEFAULT_INTERVAL)
        self.assertTrue(result["valid"])
        self.assertEqual(result["age_s"], 3 * DEFAULT_INTERVAL)

    def test_one_second_past_the_bound_is_invalid(self):
        age = 3 * DEFAULT_INTERVAL + 1
        result = _health(now=NOW, last_success=NOW - age)
        self.assertFalse(result["valid"])
        self.assertEqual(result["age_s"], age)
        self.assertEqual(result["stale_after_s"], 135)
        self.assertEqual(result["reason"], f"no successful refresh for {age}s")

    def test_failures_invalidate_a_recent_success(self):
        result = _health(now=NOW, last_success=NOW - 1.0, consecutive_failures=2)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "2 consecutive failures")
        # The age is still reported: the attributes must not hide the fact that
        # a refresh did succeed recently, only that the loop is failing now.
        self.assertEqual(result["age_s"], 1)

    def test_failures_are_named_even_when_the_success_is_also_stale(self):
        """The observation wins over the inference.

        A counted failure says what is wrong; "no successful refresh for Ns"
        only says what it looks like. Both are off, so the reason should be the
        one that names a cause.
        """
        result = _health(
            now=NOW, last_success=NOW - 10_000.0, consecutive_failures=3
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "3 consecutive failures")

    def test_a_live_connection_is_valid_without_a_recent_mint(self):
        """Mint-on-demand: while connected there are no mints to stamp the sensor."""
        result = _health(
            last_success=NOW - 600,
            connected=True,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], health.REASON_CONNECTED)

    def test_connected_with_no_success_ever_is_still_valid(self):
        result = _health(last_success=None, connected=True)
        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], health.REASON_CONNECTED)

    def test_a_refusal_invalidates_even_a_live_connection(self):
        result = _health(connected=True, consecutive_failures=1)
        self.assertFalse(result["valid"])

    def test_disconnected_still_uses_the_staleness_rule(self):
        result = _health(last_success=NOW - 600, connected=False)
        self.assertFalse(result["valid"])

    def test_never_succeeded_says_so(self):
        result = _health(last_success=None)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["age_s"])
        self.assertEqual(result["reason"], "no successful refresh yet")
        self.assertEqual(result["stale_after_s"], 3 * DEFAULT_INTERVAL)

    def test_stale_after_scales_with_the_observed_interval(self):
        for interval, expected in ((45, 135), (15, 45), (60, 180), (300, 900)):
            with self.subTest(interval=interval):
                result = _health(interval=interval)
                self.assertEqual(result["stale_after_s"], expected)

    def test_an_interval_of_zero_cannot_call_anything_fresh(self):
        """No configured loop means no healthy loop to report."""
        result = _health(now=NOW, last_success=NOW - 1.0, interval=0)
        self.assertFalse(result["valid"])
        self.assertEqual(result["stale_after_s"], 0)
        self.assertEqual(result["reason"], "no successful refresh for 1s")

    def test_junk_inputs_degrade_instead_of_raising(self):
        """These are read from a live instance, mid-write and possibly broken.

        An entity that raises while Home Assistant asks for its state is worse
        than one that says "off": the traceback lands in the log once per write
        and the entity stops updating at all.
        """
        for last_success in ("nonsense", float("nan"), float("inf"), object(), True):
            with self.subTest(last_success=last_success):
                result = _health(last_success=last_success)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], "no successful refresh yet")
        for failures in (None, "nonsense", float("nan"), float("inf")):
            with self.subTest(failures=failures):
                self.assertTrue(
                    _health(consecutive_failures=failures)["valid"],
                    "an unreadable failure count must not manufacture a failure",
                )
        for interval in (None, "nonsense", float("nan")):
            with self.subTest(interval=interval):
                self.assertEqual(_health(interval=interval)["stale_after_s"], 0)

    def test_the_returned_shape_is_the_documented_one(self):
        self.assertEqual(
            set(_health()),
            {"valid", "age_s", "stale_after_s", "reason"},
        )


class TestIso(unittest.TestCase):
    def test_iso_round_trips_and_tolerates_nothing_to_format(self):
        text = health.iso(0)
        self.assertIsNotNone(text)
        self.assertEqual(
            datetime.fromisoformat(text).timestamp(), 0,
            "the attribute must be a parseable ISO 8601 instant",
        )
        self.assertIsNone(health.iso(None))
        self.assertIsNone(health.iso("nonsense"))


class TestAgeSeconds(unittest.TestCase):
    """The arithmetic behind `last_state_message_age_s` / `last_message_age_s`.

    Both feed an attribute on the mqtt-connected sensor, so the numbers an
    operator reads there are the numbers pinned here.
    """

    def test_a_normal_age_is_truncated_toward_zero(self):
        self.assertEqual(health.age_seconds(1000.0, 990.0), 10)
        self.assertEqual(health.age_seconds(1000.9, 990.0), 10)

    def test_nothing_to_measure_reads_zero(self):
        for timestamp in (None, "nonsense", float("nan"), float("inf")):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(health.age_seconds(1000.0, timestamp), 0)
        # An unusable `now` is not a measurement either; treating it as the epoch
        # would publish a plausible-looking negative age instead of "unknown".
        self.assertEqual(health.age_seconds(float("inf"), 990.0), 0)
        self.assertEqual(health.age_seconds(None, 990.0), 0)

    def test_a_broken_clock_reads_negative_rather_than_zero(self):
        """A future timestamp must look wrong, not look like "just now".

        Clamping to 0 would turn the one visible symptom of a clock problem into
        the one value that means "healthy and chatty".
        """
        self.assertEqual(health.age_seconds(1000.0, 1010.0), -10)


class TestTranslationNames(unittest.TestCase):
    """A translation key with no name is an entity with no name."""

    KEYS = {
        "binary_sensor": ("credentials_valid", "mqtt_connected"),
        "sensor": ("last_data_update",),
    }

    def test_every_new_key_has_a_name_in_its_domain(self):
        for domain, keys in self.KEYS.items():
            names = _entity_names(domain)
            for key in keys:
                with self.subTest(domain=domain, key=key):
                    self.assertIn(
                        key, names,
                        f"entity.{domain}.{key}.name is missing from "
                        "translations/en.json — the entity would be unnamed",
                    )
                    self.assertTrue(str(names[key] or "").strip())

    def test_strings_json_agrees_with_en_json(self):
        """strings.json carries the same entity names, so it must be updated too.

        Missing it does not fail any import: the entity is named in the UI from
        translations/ and, for the integration's own source-of-truth, from
        strings.json — so a name added to one and not the other drifts silently.
        """
        for domain, keys in self.KEYS.items():
            en = _entity_names(domain)
            strings = _entity_names(domain, "strings.json")
            for key in keys:
                with self.subTest(domain=domain, key=key):
                    self.assertEqual(strings.get(key), en.get(key))

    def test_no_two_entity_names_slug_to_the_same_id_suffix(self):
        """Guard the trap this feature actually fell into.

        Home Assistant derives an entity's object id from the translated *name*
        (device-prefixed), not from the translation key. So two names that slug
        to the same suffix collide, and the second entity is created as
        `..._<suffix>_2` — an id nobody would guess and no automation names.

        This is not hypothetical: `entity.sensor.last_update` is named "Last data
        update", so the id `sensor.xpeng_<car>_last_data_update` is already taken.
        That is why the new timestamp sensor is named "Last data timestamp" and
        lives at `..._last_data_timestamp` — close enough to mean the same thing,
        and unambiguous.
        """
        for domain in self.KEYS:
            seen: dict = {}
            for key, name in _entity_names(domain).items():
                if not name:
                    continue
                suffix = _slug(name)
                other = seen.get(suffix)
                self.assertIsNone(
                    other,
                    f"entity.{domain}: {key!r} is named {name!r}, which slugs to "
                    f"{suffix!r} — already used by {other!r}. One of them would "
                    "be created as ..._2",
                )
                seen[suffix] = key


class TestShippedWiring(unittest.TestCase):
    """The rule under test must be the rule that ships."""

    def test_health_module_has_no_home_assistant_import(self):
        self.assertFalse(
            _imports_homeassistant(_HEALTH_PATH),
            "health.py must stay importable in a bare interpreter",
        )

    def test_the_entities_call_the_tested_rule(self):
        source = _source("binary_sensor.py")
        self.assertIn(
            "health.credentials_health", source,
            "the credentials-valid sensor must call health.credentials_health, "
            "not reimplement the staleness rule the tests pin",
        )
        self.assertIn("from . import health", source)
        self.assertIn(
            "health.age_seconds", source,
            "the message-age attributes must publish the arithmetic the tests "
            "cover, not a second copy of it",
        )

    def test_entity_classes_use_the_translated_keys(self):
        cases = (
            ("binary_sensor.py", "XPengCredentialsValidBinarySensor",
             "credentials_valid"),
            ("binary_sensor.py", "XPengMqttConnectedBinarySensor",
             "mqtt_connected"),
            ("sensor.py", "XPengLastDataUpdateSensor", "last_data_update"),
        )
        for filename, klass, key in cases:
            with self.subTest(klass=klass):
                self.assertEqual(
                    _class_attr(filename, klass, "_attr_translation_key"), key
                )

    def test_all_three_are_diagnostic(self):
        cases = (
            ("binary_sensor.py", "XPengCredentialsValidBinarySensor"),
            ("binary_sensor.py", "XPengMqttConnectedBinarySensor"),
            ("sensor.py", "XPengLastDataUpdateSensor"),
        )
        for filename, klass in cases:
            with self.subTest(klass=klass):
                self.assertEqual(
                    _class_attr(filename, klass, "_attr_entity_category"),
                    "EntityCategory.DIAGNOSTIC",
                    "diagnostics must not be presented as vehicle telemetry",
                )

    def test_last_data_update_is_a_timestamp_sensor(self):
        self.assertEqual(
            _class_attr("sensor.py", "XPengLastDataUpdateSensor",
                        "_attr_device_class"),
            "SensorDeviceClass.TIMESTAMP",
        )

    def test_the_setup_functions_add_the_new_entities(self):
        binary = _names_used_in("binary_sensor.py", "async_setup_entry")
        for klass in ("XPengCredentialsValidBinarySensor",
                      "XPengMqttConnectedBinarySensor"):
            self.assertIn(klass, binary, f"{klass} is never added to Home Assistant")
        self.assertIn(
            "XPengLastDataUpdateSensor",
            _names_used_in("sensor.py", "async_setup_entry"),
        )

    def test_health_entities_read_only_public_client_state(self):
        """A platform module has no business reaching into the client's guts."""
        source = _source("binary_sensor.py")
        for public in ("last_state_msg_ts", "last_msg_ts", "connected"):
            self.assertIn(public, source)
        self.assertNotIn(
            "mqtt_client._", source,
            "the health entities must use the client's public properties",
        )

    def test_credential_age_is_stamped_only_by_a_successful_mint(self):
        """`credential_timestamp` means "the last mint that produced a password".

        Two ways it stopped meaning that, each of which left the credential-age
        sensor reassuring while the loop was broken:

        * `async_setup_entry` stamped it unconditionally, so a *failed* startup
          mint looked like a successful refresh;
        * the periodic refresh stamped it inside `if pw_changed or cid_changed`,
          so a mint that returned the password already in hand left the age
          climbing through a perfectly healthy loop.

        Pinned structurally: the only stamps allowed are inside the two
        functions that mint, and never underneath the rotation guard.
        """
        tree = ast.parse(_source("__init__.py"))

        class Finder(ast.NodeVisitor):
            """Collect every stamp with its enclosing function and if-tests."""

            def __init__(self) -> None:
                self.functions: list[str] = []
                self.guards: list[str] = []
                self.stamps: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

            def visit_FunctionDef(self, node):
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_If(self, node):
                self.guards.append(ast.unparse(node.test))
                self.generic_visit(node)
                self.guards.pop()

            def visit_Call(self, node):
                if getattr(node.func, "attr", "") == "set_credential_timestamp":
                    self.stamps.append((tuple(self.functions), tuple(self.guards)))
                self.generic_visit(node)

        finder = Finder()
        finder.visit(tree)
        self.assertTrue(finder.stamps, "nothing stamps the credential timestamp")

        for functions, guards in finder.stamps:
            where = functions[-1] if functions else "<module level>"
            with self.subTest(where=where, guards=guards):
                self.assertIn(
                    where,
                    (
                        "_refresh_mqtt_creds",
                        "_handle_auth_failure",
                        # Mint-on-demand: the provider only schedules this with a
                        # minted password in hand, so it is a success site too.
                        "_persist_minted_credentials",
                    ),
                    f"credential_timestamp is stamped at {where}, which is not a "
                    "site where a mint succeeded",
                )
                self.assertFalse(
                    [guard for guard in guards if "cid_changed" in guard],
                    "stamping under `pw_changed or cid_changed` leaves the "
                    "credential age climbing whenever XPENG returns the same "
                    "password string",
                )

    def test_the_shipped_interval_is_consts_default(self):
        """At default options the bound is 3 * 45 = 135s — the documented promise."""
        default = _const_int("DEFAULT_MQTT_REFRESH_INTERVAL")
        self.assertEqual(default, DEFAULT_INTERVAL)
        self.assertEqual(health.STALE_INTERVALS, 3)
        self.assertEqual(
            _health(now=NOW, last_success=NOW)["stale_after_s"], 3 * default
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
