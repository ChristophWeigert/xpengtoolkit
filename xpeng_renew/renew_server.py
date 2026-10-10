#!/usr/bin/env python3
"""Renew XPENG credentials unattended.

Triggered by a Home Assistant automation (see docs/UNATTENDED_RENEWAL.md). One
call does what a person would: clear the login slider with the solver, mint a fresh
token + tenant pair, then hand it to the integration's ``xpeng.import_credentials``
service, which validates it before swapping anything.

The login code is the toolkit's own, vendored into vendor/ by
tools/sync_renew_vendor.py - the same code, so a renewal cannot take a different
path from a manual onboarding.

   POST /renew    run a renewal (bearer renew_token if set)
   GET  /healthz  counters and the last outcome
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ROOT, "vendor")
TOOL = os.path.join(VENDOR, "tools", "onboard_email_login.py")
PAIR_SOURCE = os.path.join(VENDOR, "custom_components", "xpeng")
DEVICE_ID_FILE = "/data/device_id.txt"
NOTIFY_AFTER = 2


def _options() -> dict:
    path = os.environ.get("OPTIONS_PATH", "/data/options.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001 - absent outside the Supervisor
        return {}


OPTIONS = _options()


def _setting(name: str, default: str = "") -> str:
    return str(os.environ.get(name.upper()) or OPTIONS.get(name) or default).strip()


EMAIL = _setting("email")
PASSWORD = _setting("password")
DEVICE_ID = _setting("device_id")
SOLVER_URL = _setting("solver_url")
SOLVER_TOKEN = _setting("solver_token")
RENEW_TOKEN = _setting("renew_token")
PAIR_DIR = _setting("pair_dir", "/config/xpeng-secrets")
CORE_API = _setting("core_api", "http://supervisor/core/api")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

STATE = {
    "started_at": time.time(),
    "renewals": 0,
    "failures": 0,
    "consecutive_failures": 0,
    "running": False,
    "last": None,
}
LOCK = threading.Lock()


def _log(message: str) -> None:
    print("[renew] %s" % message, flush=True)


def _device_id() -> str:
    """Keep one identity across renewals.

    A renewal is a login, so it mints a device id if none is given - and a fresh
    random one every time would keep changing the identity XPENG sees. Configure
    device_id, or the first renewal's value is remembered here.
    """
    if DEVICE_ID:
        return DEVICE_ID
    try:
        with open(DEVICE_ID_FILE, encoding="utf-8") as handle:
            remembered = handle.read().strip()
        if remembered:
            return remembered
    except FileNotFoundError:
        pass
    return ""


def _tail(primary: str, secondary: str = "", lines: int = 4, limit: int = 400) -> str:
    """Last non-empty ``lines`` of ``primary``, falling back to ``secondary``.

    Both streams matter. The tool writes its progress to stdout and its
    traceback to stderr, so reading only one of them hides exactly the half that
    explains a failure.

    A message longer than ``limit`` keeps its END. The specific part of a
    traceback, or of a browser launch failure, is at the bottom; trimming from
    the front threw away exactly the line that named the cause.
    """
    for text in (primary, secondary):
        kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
        if kept:
            joined = " | ".join(kept)
            if len(joined) <= limit:
                return joined
            return "..." + joined[-(limit - 3):]
    return ""


def _run_log_path() -> str:
    """Where the last run's full output is kept."""
    parent = os.path.dirname(PAIR_DIR.rstrip("/")) or "/config"
    return os.path.join(parent, "xpeng-renew-last-run.log")


def _write_run_log(stdout: str, stderr: str) -> None:
    """Keep the onboarding tool's whole output in a readable file.

    The health endpoint can only carry a summary, and the useful part of a
    browser failure is long: Playwright prints the failing line, then a boxed
    hint. Chasing Chromium through truncated summaries cost several releases,
    so every run leaves its full output here instead. Written next to the pair
    the add-on publishes, which is inside the mapped config directory.
    """
    path = _run_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Full output of the onboarding tool from the last renewal.\n"
                "# Treat as sensitive: it describes the account's login attempts.\n"
                "# stdout\n%s\n# stderr\n%s\n" % (stdout, stderr)
            )
        os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail a run
        _log("could not write %s: %s" % (path, exc))


def _mint_pair() -> tuple[bool, str]:
    """Run the toolkit's onboarding tool, which writes the pair next to itself."""
    cmd = [
        sys.executable, TOOL,
        "--email", EMAIL,
        "--password", PASSWORD,
        "--tools-dir", os.path.join(VENDOR, "tools"),
        "--device-id-out", DEVICE_ID_FILE,
        # guardian_token.mjs resolves playwright-core from
        # <playwright-dir>/node_modules, and this image installs it beside our
        # own code (see Dockerfile). The tool's default, /tmp/pwdir, has none,
        # which fails the run after the slider has already been solved.
        "--playwright-dir", ROOT,
        # The add-on has no display: the Guardian helper's default headful
        # launch fails outright with "launched a headed browser without having a
        # XServer running", which is what ended every unattended renewal right
        # after the slider had been solved.
        "--guardian-headless",
        "--save", "--mqtt",
    ]
    device_id = _device_id()
    if device_id:
        cmd += ["--device-id", device_id]
    if SOLVER_URL:
        cmd += ["--captcha-solver", SOLVER_URL]
        if SOLVER_TOKEN:
            cmd += ["--captcha-solver-token", SOLVER_TOKEN]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    _write_run_log(proc.stdout or "", proc.stderr or "")
    # On failure stderr holds the traceback, and stdout holds only the progress
    # lines printed before it died. Reporting stdout alone is what reduced a
    # missing-certificate failure to a bare "[0] device id = ..." and sent the
    # 2026-10-10 investigation looking for a request that never arrived.
    if proc.returncode != 0:
        return False, "onboarding failed: " + _tail(
            proc.stderr, proc.stdout, lines=5, limit=900
        )
    return True, _tail(proc.stdout, proc.stderr, lines=3, limit=900)


def _publish_pair() -> str:
    """Copy the pair somewhere Home Assistant's Core process can read it."""
    os.makedirs(PAIR_DIR, exist_ok=True)
    for name in ("xpeng_tokens.json", "xpeng_tenant.json"):
        destination = os.path.join(PAIR_DIR, name)
        shutil.copyfile(os.path.join(PAIR_SOURCE, name), destination)
        os.chmod(destination, 0o600)
    return PAIR_DIR


def _call_service(path: str, payload: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        CORE_API + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + SUPERVISOR_TOKEN,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, response.read().decode()[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def _notify(title: str, message: str) -> None:
    _call_service(
        "/services/persistent_notification/create",
        {"title": title, "message": message, "notification_id": "xpeng_renewal"},
    )


def renew() -> dict:
    """One renewal, start to finish. Never raises."""
    started = time.time()
    if not EMAIL or not PASSWORD:
        return {"ok": False, "error": "email/password are not configured"}

    try:
        ok, detail = _mint_pair()
        if not ok:
            return {"ok": False, "error": detail, "ms": int((time.time() - started) * 1000)}

        pair_dir = _publish_pair()
        status, body = _call_service(
            "/services/xpeng/import_credentials",
            {
                "tokens_path": os.path.join(pair_dir, "xpeng_tokens.json"),
                "tenant_path": os.path.join(pair_dir, "xpeng_tenant.json"),
            },
        )
        if status != 200:
            return {
                "ok": False,
                "error": "import_credentials returned %s: %s" % (status, body),
                "ms": int((time.time() - started) * 1000),
            }
        return {
            "ok": True,
            "imported": body,
            "onboarding": detail,
            "ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "onboarding timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _run_locked() -> dict:
    if not LOCK.acquire(blocking=False):
        return {"ok": False, "error": "a renewal is already running"}
    try:
        STATE["running"] = True
        result = renew()
        STATE["renewals"] += 1
        STATE["last"] = {**result, "at": time.time()}
        if result["ok"]:
            STATE["consecutive_failures"] = 0
            _log("renewal ok in %sms" % result.get("ms"))
            _notify("XPENG credentials renewed", "A fresh token pair was imported.")
        else:
            STATE["failures"] += 1
            STATE["consecutive_failures"] += 1
            _log("renewal failed: %s" % result.get("error"))
            if STATE["consecutive_failures"] >= NOTIFY_AFTER:
                _notify(
                    "XPENG renewal failing",
                    "The last %d renewal attempts failed. Most recent reason: %s"
                    % (STATE["consecutive_failures"], str(result.get("error"))[:300]),
                )
        return result
    finally:
        STATE["running"] = False
        LOCK.release()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/healthz", ""):
            return self._json(404, {"ok": False, "error": "GET /healthz"})
        return self._json(200, {
            "ok": True,
            "uptime_s": int(time.time() - STATE["started_at"]),
            "renewals": STATE["renewals"],
            "failures": STATE["failures"],
            "consecutive_failures": STATE["consecutive_failures"],
            "running": STATE["running"],
            "last": STATE["last"],
            "configured": {
                "email": EMAIL,
                "solver_url": SOLVER_URL,
                "pair_dir": PAIR_DIR,
                "device_id": "configured" if DEVICE_ID else "remembered/generated",
                "auth": "bearer" if RENEW_TOKEN else "open",
            },
        })

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/renew":
            return self._json(404, {"ok": False, "error": "POST /renew"})
        if RENEW_TOKEN and self.headers.get("Authorization") != "Bearer " + RENEW_TOKEN:
            return self._json(401, {"ok": False, "error": "unauthorised"})
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        result = _run_locked()
        return self._json(200 if result["ok"] else 502, result)

    def log_message(self, fmt, *args):  # route access logs through our prefix
        _log(fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or OPTIONS.get("port") or 8090)
    _log("listening on :%d (email=%s solver=%s auth=%s)"
         % (port, EMAIL or "<unset>", SOLVER_URL or "<unset>",
            "bearer" if RENEW_TOKEN else "open"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
