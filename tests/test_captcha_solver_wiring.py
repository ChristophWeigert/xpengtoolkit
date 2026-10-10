"""The hosted solver path: one HTTP call, no browser, same contract.

`--captcha-solver` is what makes onboarding unattended, so the request shape
(path, auth header, timeout) and the fallback ordering are both worth pinning.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "onboard_email_login.py"
SOURCE = TOOL.read_text(encoding="utf-8")

try:
    _spec = importlib.util.spec_from_file_location("onboard_email_login", TOOL)
    tool = importlib.util.module_from_spec(_spec)
    sys.modules["onboard_email_login"] = tool
    _spec.loader.exec_module(tool)
    IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - missing cryptography/HA modules are fine
    tool = None
    IMPORT_ERROR = exc


class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        if self.status != 200:
            payload = {"ok": False, "error": "solver said no"}
        else:
            payload = {
                "ok": True,
                "validate": "V" * 531,
                "captchaId": "f6c14777879d4a75b683b29be6709db6",
                "attempts": 2,
                "ms": 14000,
            }
        body = json.dumps(payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


def _serve(status=200):
    _Handler.seen = []
    _Handler.status = status
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@unittest.skipIf(tool is None, f"tool not importable ({IMPORT_ERROR})")
class SolverServiceTests(unittest.TestCase):
    def test_success_returns_the_validate_and_sends_the_token(self):
        server, url = _serve()
        try:
            result = tool.solve_via_service(url, "s3cret")
        finally:
            server.shutdown()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["validate"]), 531)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(_Handler.seen[0][0], "/solve")
        self.assertEqual(_Handler.seen[0][1], "Bearer s3cret")

    def test_no_token_means_no_authorization_header(self):
        server, url = _serve()
        try:
            tool.solve_via_service(url)
        finally:
            server.shutdown()
        self.assertIsNone(_Handler.seen[0][1])

    def test_a_refused_solve_is_reported_not_raised(self):
        server, url = _serve(status=502)
        try:
            result = tool.solve_via_service(url)
        finally:
            server.shutdown()
        self.assertFalse(result["ok"])
        # The service's own message must survive: "solver said no" is what tells a
        # user whether the drag failed or the widget refused the solve.
        self.assertIn("solver said no", json.dumps(result))

    def test_an_unreachable_service_is_reported(self):
        result = tool.solve_via_service("http://127.0.0.1:1", timeout=2)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class SolverWiringTests(unittest.TestCase):
    """Text-level checks: the parser lives inside main(), so it is not importable."""

    def test_the_flag_and_env_var_exist(self):
        self.assertIn('"--captcha-solver"', SOURCE)
        self.assertIn("XPENG_CAPTCHA_SOLVER", SOURCE)
        self.assertIn("XPENG_CAPTCHA_SOLVER_TOKEN", SOURCE)

    def test_the_browser_is_only_the_fallback(self):
        self.assertIn("if args.captcha_solver:", SOURCE)
        self.assertLess(
            SOURCE.index("if args.captcha_solver:"),
            SOURCE.index('cmd = ["node", str(solve_js)'),
            "the local browser drag must sit in the else branch, not before it",
        )

    def test_the_service_path_still_requires_the_guardian_helper(self):
        branch = SOURCE[SOURCE.index("if args.captcha_solver:"):]
        branch = branch[: branch.index("else:")]
        self.assertIn("guardian_js", branch)

    def test_no_browser_helper_is_required_when_the_solver_is_used(self):
        branch = SOURCE[SOURCE.index("if args.captcha_solver:"):]
        branch = branch[: branch.index("else:")]
        self.assertNotIn("solve_captcha.mjs", branch)
        self.assertNotIn("run_json", branch)


if __name__ == "__main__":
    unittest.main()
