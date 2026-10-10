#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The mTLS certificate directory must resolve for every layout that loads us.

Three layouts load ``xpeng_api.py`` and they keep the certs in different places:

* Home Assistant imports it as a package, so ``const.py`` answers with the
  config directory.
* A development checkout loads it by path with the component directory off
  ``sys.path``, so ``const.py`` is not importable and the certs sit beside the
  module.
* The renewal add-on vendors the module and ships no certs of its own: it reads
  the integration's from ``/config`` through its ``config:rw`` map.

Resolution used to trust a single hardcoded answer, so the add-on looked for
certs next to its own vendored copy, found none, and died at "mTLS certificate
file(s) missing" before it opened a socket -- on every renewal, for as long as
the add-on existed (found 2026-10-10). Home Assistant never reached that branch,
so the one broken layout was the one the suite never loaded.

The certificates are not in the repository and must not be, so the filesystem is
faked below. What matters is which candidate is chosen, not that a real login
works.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

_MODULE_PATH = _component.module_path("xpeng_api.py")
try:
    _spec = importlib.util.spec_from_file_location("xpeng_api_certdir", _MODULE_PATH)
    assert _spec and _spec.loader
    api = importlib.util.module_from_spec(_spec)
    sys.modules["xpeng_api_certdir"] = api
    _spec.loader.exec_module(api)
except ImportError as exc:  # cryptography is the module's only third-party import
    raise unittest.SkipTest(f"xpeng_api.py could not be imported: {exc}")

HA_CERTS = "/config/custom_components/xpeng/certs"
BESIDE_MODULE = str(pathlib.Path(_MODULE_PATH).resolve().parent / "certs")
#: The location the README recommends, because it survives an integration update.
RECOMMENDED = "/config/xpeng-certs"


class _FakeCaChain:
    """Report a ca-chain.crt only in the given directories.

    Every other ``ca-chain.crt`` is reported absent, so a real installation
    sitting beside the module (the private working copy) cannot decide the
    outcome of a test that is about a different layout.
    """

    def __init__(self, *directories):
        self.present = {os.path.normpath(d) for d in directories}

    def __enter__(self):
        self._real = os.path.isfile
        present = self.present
        real = self._real

        def isfile(path):
            normalised = os.path.normpath(str(path))
            if normalised.endswith("ca-chain.crt"):
                return os.path.dirname(normalised) in present
            return real(path)

        os.path.isfile = isfile
        return self

    def __exit__(self, *exc):
        os.path.isfile = self._real
        return False


class TestCertificateDirectory(unittest.TestCase):
    def test_only_the_config_directory_has_certs(self):
        """The renewal add-on: a vendored module that carries no certs itself."""
        with _FakeCaChain(HA_CERTS):
            self.assertEqual(api._default_cert_dir(), HA_CERTS)

    def test_certs_beside_the_module_win(self):
        """A development checkout: the copy next to the module is complete."""
        with _FakeCaChain(BESIDE_MODULE, HA_CERTS):
            self.assertEqual(api._default_cert_dir(), BESIDE_MODULE)

    def test_no_certs_anywhere_still_names_a_directory(self):
        """Installing nothing yet must leave a usable path in the error text."""
        with _FakeCaChain():
            chosen = api._default_cert_dir()
        self.assertTrue(chosen, "the error message would name an empty path")
        self.assertTrue(chosen.endswith("certs"), chosen)

    def test_the_module_default_comes_from_the_resolver(self):
        self.assertEqual(api.DEFAULT_CERT_DIR, api._default_cert_dir())

    def test_the_update_safe_directory_needs_no_option(self):
        """Certs kept outside the integration resolve without setting cert_dir.

        That is the layout the README recommends: an integration update reinstalls
        ``custom_components/xpeng/`` and takes anything stored inside it with it,
        so keys there are one HACS update away from vanishing.
        """
        with _FakeCaChain(RECOMMENDED):
            self.assertEqual(api._default_cert_dir(), RECOMMENDED)

    def test_an_existing_default_still_wins_over_the_new_candidate(self):
        """The new location is tried last, so it cannot move a working install.

        Anything already resolving to the directory inside the integration must
        keep resolving to exactly that, or upgrading would change which keys an
        install uses.
        """
        with _FakeCaChain(HA_CERTS, RECOMMENDED):
            self.assertEqual(api._default_cert_dir(), HA_CERTS)


class TestRenewAddonReportsStderr(unittest.TestCase):
    """The add-on must not reduce a traceback to the last progress line.

    ``_mint_pair`` read ``proc.stdout or proc.stderr``, so whenever the tool had
    printed anything (it always has) the traceback explaining the failure was
    thrown away. That is why this bug presented as "the call never arrived".
    """

    @classmethod
    def setUpClass(cls):
        path = _component.ROOT / "xpeng_renew" / "renew_server.py"
        if not path.is_file():
            raise unittest.SkipTest(f"{path} is not in this tree")
        spec = importlib.util.spec_from_file_location("renew_server_t", path)
        assert spec and spec.loader
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def test_a_failure_prefers_stderr(self):
        stdout = "[0] device id = " + "a" * 32
        stderr = "Traceback (most recent call last):\nFileNotFoundError: certs"
        reported = self.server._tail(stderr, stdout, lines=2)
        self.assertIn("FileNotFoundError", reported)
        self.assertNotIn("[0] device id", reported)

    def test_a_success_still_reports_progress(self):
        self.assertIn("[8] createMqttAccount", self.server._tail("[8] createMqttAccount OK", ""))

    def test_a_long_failure_keeps_its_end(self):
        """The cause sits at the bottom of a long browser-launch message.

        Trimming from the front cut the one line that named it, which is how a
        missing Chromium sandbox flag presented as an unreadable wall of box
        drawing.
        """
        stderr = "\n".join(f"noise line {n}" for n in range(40)) + \
            "\n" + "x" * 120 + " sandbox flags missing"
        reported = self.server._tail(stderr, "", lines=1, limit=60)
        self.assertIn("sandbox flags missing", reported)
        self.assertLessEqual(len(reported), 60)
        self.assertTrue(reported.startswith("..."))

    def test_nothing_at_all_is_not_an_error(self):
        self.assertEqual(self.server._tail("", ""), "")


if __name__ == "__main__":
    unittest.main()
