#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the shipped translation files. Standard library only.

    python3 tests/test_translations.py
    python3 -m unittest discover -s tests -v

Why these exist: Home Assistant merges translations per key against English, so
every way a language file can be wrong is silent at runtime. A missing key shows
English on one screen; a mistyped placeholder is dropped by Home Assistant and
shows English with only a log line; a language file that is a copy of English
"works" perfectly and helps nobody. These tests are the only place those
failures are loud.

The entity-name tests guard the other silent failure: Home Assistant derives an
entity id from the *name*, and ``XPengEntity.suggested_object_id`` pins that to
the English name. If two English names slug to the same string the second entity
lands on ``_2``; if a name contains a placeholder the pinned id would be a
half-substituted string.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "xpeng"
TRANSLATIONS = COMPONENT / "translations"
STRINGS = COMPONENT / "strings.json"
ENGLISH = TRANSLATIONS / "en.json"

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402
import check_translations  # noqa: E402


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Loaded by path, not imported as `custom_components.xpeng.entity_names`: that
# would execute the package's __init__, which imports Home Assistant, which is
# not installed here and must not need to be. ``entity_names`` is deliberately
# free of HA imports - that is why the pinned names live there.
_SPEC = importlib.util.spec_from_file_location(
    "xpeng_entity_names", _component.module_path("entity_names.py")
)
assert _SPEC and _SPEC.loader
entity_names = importlib.util.module_from_spec(_SPEC)
sys.modules["xpeng_entity_names"] = entity_names
_SPEC.loader.exec_module(entity_names)


def _languages() -> list[pathlib.Path]:
    return sorted(
        path for path in TRANSLATIONS.glob("*.json") if path.stem not in {"en", "strings"}
    )


def _slug(name: str) -> str:
    """Home Assistant's object-id slug, near enough for the ASCII names in use."""
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")


class TestShippedTranslations(unittest.TestCase):
    """Every language file must be structurally complete and safe to load."""

    def test_the_checker_reports_no_errors(self):
        errors, _warnings, count = check_translations.check(COMPONENT)
        self.assertGreater(count, 300, "en.json should carry the full string set")
        self.assertEqual(errors, [], "\n".join(errors))

    def test_strings_json_is_the_english_file(self):
        """Only en.json is read at runtime, so strings.json must not drift."""
        self.assertEqual(
            json.loads(STRINGS.read_text(encoding="utf-8")),
            json.loads(ENGLISH.read_text(encoding="utf-8")),
        )

    def test_every_language_has_every_english_key(self):
        english = check_translations.flatten(_load(ENGLISH))
        for path in _languages():
            with self.subTest(language=path.stem):
                translated = check_translations.flatten(_load(path))
                missing = sorted(set(english) - set(translated))
                extra = sorted(set(translated) - set(english))
                self.assertEqual(missing, [], f"{path.name} misses {missing[:5]}")
                self.assertEqual(extra, [], f"{path.name} adds {extra[:5]}")

    def test_no_language_is_a_copy_of_english(self):
        """A file that is still English is the one failure that looks perfect."""
        english = check_translations.flatten(_load(ENGLISH))
        for path in _languages():
            with self.subTest(language=path.stem):
                translated = check_translations.flatten(_load(path))
                shared = [
                    key for key in english
                    if key in translated and english[key] == translated[key]
                ]
                # Product nouns and one-word names legitimately match; a file
                # that is mostly identical has not been translated.
                self.assertLess(
                    len(shared), len(english) / 2,
                    f"{path.name}: {len(shared)}/{len(english)} values are still "
                    "English - this is a copy, not a translation",
                )

    def test_prose_quotes_the_dropdown_labels_verbatim(self):
        """The config-flow dropdown labels are hardcoded English.

        ``_preset_options()``, ``_permission_options()`` and the sign-in-method
        selector build their labels in ``config_flow.py``, so the UI shows
        English there whatever the HA language is. Descriptions quote those
        labels; translating the quote would make the text disagree with the
        dropdown right above it. Count rather than presence: every place English
        names a label must name it the same way here.
        """
        labels = (
            "Read only",
            "Safe Plus",
            "Full access",
            "Custom",
            "Disabled",
            "Import tokens from the onboarding tool",
        )
        english = check_translations.flatten(_load(ENGLISH))
        for path in _languages():
            translated = check_translations.flatten(_load(path))
            for label in labels:
                with self.subTest(language=path.stem, label=label):
                    self.assertEqual(
                        sum(value.count(label) for value in translated.values()),
                        sum(value.count(label) for value in english.values()),
                        f"{path.name} quotes {label!r} a different number of "
                        "times than en.json - the dropdown shows the English label",
                    )

    def test_language_codes_are_ones_home_assistant_asks_for(self):
        for path in _languages():
            with self.subTest(language=path.stem):
                self.assertRegex(path.stem, check_translations.LANGUAGE_CODE)
                self.assertNotEqual(path.stem, "en")

    def test_english_covers_every_section(self):
        english = _load(ENGLISH)
        for section in ("config", "options", "entity", "issues", "exceptions"):
            with self.subTest(section=section):
                self.assertIn(section, english)


class TestEntityNames(unittest.TestCase):
    """The English names are the source of the pinned entity ids."""

    def test_every_english_entity_key_has_a_name(self):
        for platform, entries in _load(ENGLISH)["entity"].items():
            for key, entry in entries.items():
                with self.subTest(platform=platform, key=key):
                    name = (entry or {}).get("name")
                    self.assertIsInstance(name, str)
                    self.assertTrue(name.strip())

    def test_no_entity_name_contains_a_placeholder(self):
        """``suggested_object_id`` returns the raw English name."""
        for platform, entries in _load(ENGLISH)["entity"].items():
            for key, entry in entries.items():
                with self.subTest(platform=platform, key=key):
                    self.assertNotIn("{", entry.get("name", ""))

    def test_no_two_names_slug_to_the_same_entity_id(self):
        """A collision lands the second entity on ``_2`` of an id nobody writes."""
        for platform, entries in _load(ENGLISH)["entity"].items():
            seen: dict[str, str] = {}
            for key, entry in entries.items():
                suffix = _slug(entry.get("name", ""))
                if not suffix:
                    continue
                with self.subTest(platform=platform, key=key):
                    self.assertNotIn(
                        suffix, seen,
                        f"{platform}.{key} and {platform}.{seen.get(suffix)} both "
                        f"slug to {suffix!r}",
                    )
                seen[suffix] = key

    def test_pinning_reads_the_literal_it_claims_to(self):
        """Guard the data ``suggested_object_id`` depends on."""
        names = check_translations.flatten(_load(ENGLISH))
        self.assertIn("entity.sensor.battery_soc.name", names)
        self.assertIn("entity.switch.sentinel_switch.name", names)


class TestEntityIdPinning(unittest.TestCase):
    """Entity ids must not follow the UI language; only names do."""

    def test_the_loader_returns_the_english_name(self):
        self.assertEqual(
            entity_names.english_name("sensor", "battery_soc"), "Battery"
        )
        self.assertEqual(
            entity_names.english_name("switch", "sentinel_switch"), "X-Guard"
        )

    def test_unknown_keys_and_platforms_answer_none(self):
        for platform, key in (
            ("sensor", "does_not_exist"),
            ("does_not_exist", "battery_soc"),
            (None, "battery_soc"),
            ("sensor", None),
        ):
            with self.subTest(platform=platform, key=key):
                self.assertIsNone(entity_names.english_name(platform, key))

    def test_english_names_match_the_translation_file(self):
        """The pinned names are a literal so the lookup is not blocking I/O.

        A literal can drift from ``translations/en.json``, so it is checked here
        on every run. To fix a failure: copy the English name from ``en.json``
        into ``custom_components/xpeng/entity_names.py``.
        """
        english = {
            platform: {key: entry["name"] for key, entry in entries.items()}
            for platform, entries in _load(ENGLISH)["entity"].items()
        }
        self.assertEqual(entity_names.ENGLISH_ENTITY_NAMES, english)

    def test_every_entity_name_is_reachable_through_the_loader(self):
        english = _load(ENGLISH)["entity"]
        loaded = entity_names.ENGLISH_ENTITY_NAMES
        for platform, entries in english.items():
            with self.subTest(platform=platform):
                self.assertEqual(
                    {key for key in entries}, set(loaded.get(platform, {}))
                )

    def test_entity_py_actually_uses_the_pinning_loader(self):
        """The property is what Home Assistant reads; the loader is only a helper."""
        source = _component.module_path("entity.py").read_text(encoding="utf-8")
        self.assertIn("def suggested_object_id(self)", source)
        self.assertIn("english_name(self.platform.domain, key)", source)
        self.assertIn("return super().suggested_object_id", source)


class TestNameCoverage(unittest.TestCase):
    """A hardcoded ``_attr_name`` wins over the translation key, silently."""

    #: Every module that adds entities. ``_attr_name`` here means the entity's
    #: friendly name ignores the active language, which is how the UI ends up
    #: half-translated.
    PLATFORMS = (
        "sensor.py",
        "binary_sensor.py",
        "button.py",
        "calendar.py",
        "climate.py",
        "cover.py",
        "device_tracker.py",
        "image.py",
        "lock.py",
        "number.py",
        "select.py",
        "switch.py",
    )

    def test_platforms_do_not_set_a_name_that_beats_the_translation(self):
        for filename in self.PLATFORMS:
            with self.subTest(platform=filename):
                source = _component.module_path(filename).read_text(encoding="utf-8")
                offenders = [
                    f"{number}: {line.strip()}"
                    for number, line in enumerate(source.splitlines(), 1)
                    if "_attr_name" in line
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{filename} sets _attr_name; Home Assistant prefers it over the "
                    "translation key, so the name would stay English",
                )


if __name__ == "__main__":
    unittest.main()
