"""The renewal add-on runs copies of the login code - they must not drift.

An add-on's build context is its own folder, so xpeng_renew/vendor/ holds generated
copies. These tests fail the moment someone edits the real module without re-running
tools/sync_renew_vendor.py.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
VENDOR = REPO / "xpeng_renew" / "vendor"
COPIED = [
    "custom_components/xpeng/xpeng_api.py",
    "custom_components/xpeng/xpeng_login.py",
    "custom_components/xpeng/xpeng_errors.py",
    "custom_components/xpeng/guardian.py",
    "tools/onboard_email_login.py",
    "tools/guardian_token.mjs",
]


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VendorSyncTests(unittest.TestCase):
    def test_vendored_copies_match_their_sources(self):
        for rel in COPIED:
            with self.subTest(rel=rel):
                source, copy = REPO / rel, VENDOR / rel
                self.assertTrue(copy.is_file(), f"{copy} is missing - run tools/sync_renew_vendor.py")
                self.assertEqual(
                    digest(source),
                    digest(copy),
                    f"{rel} changed but the vendored copy did not: "
                    "run tools/sync_renew_vendor.py",
                )

    def test_the_trimmed_const_covers_every_constant_the_code_uses(self):
        vendor_const = (VENDOR / "custom_components/xpeng/const.py").read_text(encoding="utf-8")
        defined = set(re.findall(r"^([A-Z][A-Z0-9_]*)(?::[^=]+)?=", vendor_const, re.M))
        needed: set[str] = set()
        for path in VENDOR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            needed |= set(re.findall(r"const\.([A-Z][A-Z0-9_]*)", text))
            for line in re.findall(r"from \.const import ([^\n(]+)", text):
                needed |= set(re.findall(r"([A-Z][A-Z0-9_]*)", line))
            for block in re.findall(r"from \.const import \(([^)]*)\)", text):
                needed |= set(re.findall(r"([A-Z][A-Z0-9_]*)", block))
        missing = sorted(needed - defined)
        self.assertEqual(missing, [], f"vendored const.py is missing {missing}")

    def test_the_vendored_const_does_not_import_home_assistant(self):
        text = (VENDOR / "custom_components/xpeng/const.py").read_text(encoding="utf-8")
        self.assertNotIn("homeassistant", text)


if __name__ == "__main__":
    unittest.main()
