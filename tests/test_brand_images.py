#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Brand images have to match Home Assistant's specification, or they are not shown.

The numbers are not cosmetic: the icon must be exactly 256x256 (512x512 for the hDPI
pair), and a logo's *shortest* side must land between 128 and 256 (256 and 512 at 2x).
A resize that ignores that is silently rejected rather than visibly broken, so it is
checked here.

The header is parsed directly - a PNG's dimensions live at bytes 16 and 20 - because the
suite has no imaging library, and adding one to check four numbers would be worse than
reading them.
"""
from __future__ import annotations

import pathlib
import struct
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "xpeng" / "brand"
SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _size(name: str) -> tuple[int, int]:
    data = (BRAND / name).read_bytes()
    if data[:8] != SIGNATURE:
        raise AssertionError(f"{name} is not a PNG")
    return struct.unpack(">II", data[16:24])


class BrandImageTests(unittest.TestCase):
    def test_the_square_icons_are_exactly_the_required_size(self):
        self.assertEqual(_size("icon.png"), (256, 256))
        self.assertEqual(_size("icon@2x.png"), (512, 512))

    def test_the_icons_are_square_and_the_logos_are_landscape(self):
        for name in ("icon.png", "icon@2x.png"):
            width, height = _size(name)
            self.assertEqual(width, height, name)
        for name in ("logo.png", "logo@2x.png"):
            width, height = _size(name)
            self.assertGreater(width, height, f"{name} should be landscape")

    def test_each_logo_shortest_side_is_within_the_allowed_range(self):
        """128-256 normal, 256-512 at 2x - a bigger logo is not accepted upstream."""
        for name, low, high in (("logo.png", 128, 256), ("logo@2x.png", 256, 512)):
            width, height = _size(name)
            shortest = min(width, height)
            self.assertGreaterEqual(shortest, low, name)
            self.assertLessEqual(shortest, high, name)

    def test_the_source_svgs_are_kept_so_the_images_can_be_changed(self):
        """Without these, changing a colour means drawing the mark again."""
        for name in ("icon.svg", "logo.svg"):
            self.assertTrue((BRAND / name).is_file(), name)

    def test_the_brand_directory_carries_an_explanation(self):
        """The mark is ours, not XPENG's; the next reader should not have to guess."""
        readme = (BRAND / "README.md").read_text(encoding="utf-8")
        self.assertIn("own", readme)
        self.assertIn("rsvg-convert", readme)


if __name__ == "__main__":
    unittest.main()
