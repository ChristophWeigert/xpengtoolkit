#!/usr/bin/env python3
"""Check every shipped translation against ``strings.json``.

Home Assistant merges translations key by key: English is loaded first and the
requested language is overlaid on top. A missing key therefore falls back to
English silently, and an extra key is simply never shown. Neither is a runtime
error, which is exactly why this check has to exist - a language that has
drifted from ``strings.json`` looks fine in Home Assistant until a user finds
the one screen that is still in English.

    python3 tools/check_translations.py            # every language
    python3 tools/check_translations.py --lang de  # one language
    python3 tools/check_translations.py --quiet    # errors only

Exit code 1 on errors. Warnings never fail the run; they are for review.

What is checked, and why key parity alone is not enough:

* **Key parity** - a missing key is a string that stays English; an extra key is
  a string no code can ever show.
* **Placeholders** - Home Assistant drops a localized string whose placeholders
  differ from English and quietly falls back to English
  (``translation.py``, ``_validate_placeholders``), so a typo is invisible at
  runtime.
* **Fenced code blocks** - the token-import walkthrough carries shell commands.
  A "translated" command is a broken instruction.
* **URLs** - a translated link is a 404.
* **Language codes** - only files Home Assistant could ask for are shipped.

Values identical to English are warnings, not errors: product nouns (XPENG,
VIN, MQTT, XOS, CDU) legitimately stay as they are in every language.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import string
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "xpeng"

#: Home Assistant language codes: an ISO 639-1 (or 639-2) code, optionally with
#: an uppercase region suffix for the few languages that need one.
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")

_FENCE = re.compile(r"```.*?```", re.S)
_URL = re.compile(r"https?://[^\s)\]`'\">]+")


def flatten(value: object, prefix: str = "") -> dict[str, str]:
    """Flatten a nested strings file to ``{"config.step.user.title": "..."}``."""
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flat.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, str):
        flat[prefix] = value
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flat.update(flatten(child, f"{prefix}.{index}"))
    return flat


def placeholders(text: str) -> set[str]:
    """The ``{name}`` fields in a string, matching Home Assistant's own check."""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        # An unbalanced brace is not a placeholder bug this tool can diagnose;
        # Home Assistant logs it and falls back to English.
        return set()
    return {field for _, field, _, _ in parsed if field is not None}


def compare(
    language: str, translated: dict[str, str], english: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for one language against English."""
    errors: list[str] = []
    warnings: list[str] = []

    for key in sorted(set(english) - set(translated)):
        errors.append(
            f"{language}: {key}: missing - Home Assistant would show English here"
        )
    for key in sorted(set(translated) - set(english)):
        errors.append(f"{language}: {key}: not in strings.json - no code can show this")

    for key in sorted(set(english) & set(translated)):
        source = english[key]
        value = translated[key]

        if not value.strip():
            errors.append(f"{language}: {key}: empty")
            continue

        if placeholders(source) != placeholders(value):
            errors.append(
                f"{language}: {key}: placeholders {sorted(placeholders(value))} "
                f"!= English {sorted(placeholders(source))} - Home Assistant would "
                "discard the translation"
            )

        if _FENCE.findall(source) != _FENCE.findall(value):
            errors.append(
                f"{language}: {key}: fenced code block differs from English - "
                "commands must stay verbatim"
            )

        if set(_URL.findall(source)) != set(_URL.findall(value)):
            errors.append(f"{language}: {key}: URL differs from English")

        if value == source and len(source) > 3:
            warnings.append(f"{language}: {key}: identical to English")

    return errors, warnings


def check(
    component: Path = COMPONENT, languages: list[str] | None = None
) -> tuple[list[str], list[str], int]:
    """Check ``component``; return ``(errors, warnings, english_string_count)``."""
    strings_path = component / "strings.json"
    translations = component / "translations"
    english_path = translations / "en.json"

    errors: list[str] = []
    warnings: list[str] = []

    try:
        strings = json.loads(strings_path.read_text(encoding="utf-8"))
        english_data = json.loads(english_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return [f"cannot read the English strings: {err}"], [], 0

    if strings != english_data:
        errors.append(
            "strings.json and translations/en.json disagree - only en.json is "
            "read at runtime, so strings.json is the copy that drifts"
        )

    english = flatten(english_data)
    if not english:
        errors.append("translations/en.json has no strings")

    files = sorted(translations.glob("*.json"))
    files = [path for path in files if path.stem not in {"en", "strings"}]
    if languages:
        wanted = set(languages)
        files = [path for path in files if path.stem in wanted]
        for missing in sorted(wanted - {path.stem for path in files}):
            errors.append(f"{missing}: translations/{missing}.json does not exist")

    for path in files:
        language = path.stem
        if not LANGUAGE_CODE.match(language):
            errors.append(
                f"{language}: {path.name} is not a Home Assistant language code"
            )
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            errors.append(f"{language}: unreadable JSON: {err}")
            continue
        language_errors, language_warnings = compare(language, flatten(data), english)
        errors.extend(language_errors)
        warnings.extend(language_warnings)

    return errors, warnings, len(english)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lang",
        action="append",
        default=None,
        help="check only these languages (repeatable); default is every file",
    )
    parser.add_argument("--quiet", action="store_true", help="print errors only")
    args = parser.parse_args(argv)

    errors, warnings, count = check(languages=args.lang)

    if not args.quiet:
        print(f"translations/en.json: {count} strings")
        for path in sorted((COMPONENT / "translations").glob("*.json")):
            if path.stem == "en" or (args.lang and path.stem not in args.lang):
                continue
            print(f"  checked {path.name}")

    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"FAIL  {error}", file=sys.stderr)

    print(
        f"{count} English strings; {len(errors)} error(s), "
        f"{len(warnings)} warning(s)"
    )
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
