#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`solver_url` must be the solver's API address, not its sidebar panel.

    python3 tests/test_solver_url.py
    python3 -m unittest discover -s tests

The address is the one setting that depends on how the solver was installed, and its
most natural wrong answer is the one the Home Assistant UI hands you: the ingress panel
URL, `/app/571f4c18_xpeng_slider_solver`. It resolves, so it does not read as a typo,
and the failure it produces - `405: Method Not Allowed`, from a POST to a GET-only
ingress route - reads as a broken solver. Reported 2026-10-13 on an install whose
renewal log said exactly that, with an empty `/config/xpeng-secrets` and no config
entry, which is what sent the investigation off after a missing credential pair.

The panel form carries the slug, so the container hostname is mechanical from it.
Anything that is not a panel URL is left exactly as given.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_server():
    """Import the app; it only starts serving under `__main__`."""
    path = ROOT / "xpeng_renew" / "renew_server.py"
    spec = importlib.util.spec_from_file_location("renew_server_solver_url", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["renew_server_solver_url"] = module
    spec.loader.exec_module(module)
    return module


class SolverUrlTests(unittest.TestCase):
    """The correction, and the promise that it does not touch a real address."""

    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def test_the_sidebar_panel_url_becomes_the_container_hostname(self):
        """The exact value from the report, which produced the 405."""
        url, note = self.server._solver_url(
            "http://homeassistant.local:8123/app/571f4c18_xpeng_slider_solver"
        )
        self.assertEqual(url, "http://571f4c18-xpeng-slider-solver:8080")
        self.assertIn("405", note)
        self.assertIn("set solver_url", note)

    def test_a_local_folder_slug_is_derived_the_same_way(self):
        url, note = self.server._solver_url(
            "http://homeassistant.local:8123/app/local_xpeng_slider_solver/"
        )
        self.assertEqual(url, "http://local-xpeng-slider-solver:8080")
        self.assertTrue(note)

    def test_a_signed_ingress_token_is_reported_rather_than_guessed(self):
        """`/api/hassio_ingress/<token>` carries no slug, so nothing may be invented."""
        raw = "http://homeassistant.local:8123/api/hassio_ingress/abc123signed/panel"
        url, note = self.server._solver_url(raw)
        self.assertEqual(url, raw)
        self.assertIn("8080", note)

    def test_a_real_address_is_never_second_guessed(self):
        for value in (
            "http://571f4c18-xpeng-slider-solver:8080",
            "http://192.168.1.10:8080",
            "http://solver.internal:9999",
            "https://solver.example.com",
        ):
            with self.subTest(value):
                self.assertEqual(self.server._solver_url(value), (value, ""))

    def test_a_trailing_slash_and_an_unset_option_are_fine(self):
        self.assertEqual(
            self.server._solver_url("http://192.168.1.10:8080/"),
            ("http://192.168.1.10:8080", ""),
        )
        self.assertEqual(self.server._solver_url(""), ("", ""))
        self.assertEqual(self.server._solver_url("   "), ("", ""))

    def test_the_app_uses_the_corrected_url_and_reports_what_it_did(self):
        source = (ROOT / "xpeng_renew" / "renew_server.py").read_text(encoding="utf-8")
        self.assertIn(
            'SOLVER_URL, SOLVER_URL_NOTE = _solver_url(_setting("solver_url"))', source
        )
        # the tool is handed the corrected value...
        self.assertIn('cmd += ["--captcha-solver", SOLVER_URL]', source)
        # ...and the correction is visible before the first renewal, not only after
        self.assertIn('_log("[!] " + SOLVER_URL_NOTE)', source)
        self.assertIn('"solver_url_note": SOLVER_URL_NOTE', source)

    def test_the_tool_refuses_a_panel_url_as_well(self):
        """For a manual `--captcha-solver`, which the app cannot correct for you.

        Checked in the source because the guard must run *before* the request: the
        point is not to describe the 405 after waiting for it.
        """
        source = (ROOT / "tools" / "onboard_email_login.py").read_text(encoding="utf-8")
        start = source.index("def solve_via_service(")
        body = source[start:start + 1600]
        self.assertIn('"/app/" in url', body)
        self.assertIn("only answers GET", body)
        self.assertLess(
            body.index('"/app/" in url'), body.index("urllib.request.Request(")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
