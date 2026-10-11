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


def _setting_int(name: str, default: int) -> int:
    try:
        return int(float(_setting(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


EMAIL = _setting("email")
PASSWORD = _setting("password")
DEVICE_ID = _setting("device_id")
SOLVER_URL = _setting("solver_url")
SOLVER_TOKEN = _setting("solver_token")
RENEW_TOKEN = _setting("renew_token")
PAIR_DIR = _setting("pair_dir", "/config/xpeng-secrets")
# An override for the VIN used to create a config entry on a fresh install. The
# onboarding tool records one in the token file when the API gave it one, so this
# is only needed for a pair written before that, or by hand.
VIN = _setting("vin")
# How often to renew, in seconds, and 0 to disable. Inside the ~2h session cap with
# room for a retry, which is the number docs/UNATTENDED_RENEWAL.md works from.
RENEW_INTERVAL = _setting_int("renew_interval", 6000)
# How often to ask Home Assistant whether the credentials are still good, so a session
# that dies early is replaced on the next check rather than at the next tick.
INVALID_POLL_INTERVAL = _setting_int("invalid_poll_interval", 300)
CORE_API = _setting("core_api", "http://supervisor/core/api")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

STATE = {
    "started_at": time.time(),
    "renewals": 0,
    "failures": 0,
    "consecutive_failures": 0,
    "running": False,
    "last": None,
    # Reported by /healthz so the schedule is as visible as it was when an
    # automation and its helper were the clock.
    "next_run": 0.0,
    "schedule_reason": "",
}
LOCK = threading.Lock()
#: Resolved once by _credentials_entity(); empty until then.
_CREDENTIALS_ENTITY: list[str] = []


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


def _core_json(path: str, payload: dict | None = None, method: str = "POST"):
    """A Core API call whose response body has to survive intact.

    _call_service() truncates to 400 characters because what it returns goes into a
    log line. That is right for an error and useless for JSON, so this keeps the
    whole body and parses it.
    """
    request = urllib.request.Request(
        CORE_API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Bearer " + SUPERVISOR_TOKEN,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def _existing_entry() -> dict | None:
    """This install's XPENG config entry, if it has one yet."""
    status, body = _core_json("/config/config_entries/entry", method="GET")
    if status != 200 or not isinstance(body, list):
        return None
    for entry in body:
        if isinstance(entry, dict) and entry.get("domain") == "xpeng":
            return entry
    return None


def _vin_from_pair(pair_dir: str) -> str:
    """The VIN recorded in the token file, or "".

    Same two shapes the integration accepts: the tool's ``json`` wrapper, or a bare
    token object. Only a real VIN is returned (17 alphanumeric ASCII characters),
    so a placeholder cannot reach the config entry.
    """
    if VIN:
        return VIN.strip().upper()
    try:
        with open(
            os.path.join(pair_dir, "xpeng_tokens.json"), encoding="utf-8"
        ) as handle:
            raw = json.load(handle)
    except Exception:  # noqa: BLE001 - an unreadable pair is reported by the import
        return ""

    inner = raw
    if isinstance(raw, dict):
        for field in ("json", "jsonText"):
            candidate = raw.get(field)
            if isinstance(candidate, dict):
                inner = candidate
                break
            if isinstance(candidate, str) and candidate.lstrip().startswith("{"):
                try:
                    inner = json.loads(candidate[candidate.find("{"):])
                except Exception:  # noqa: BLE001
                    pass
                break
    if not isinstance(inner, dict):
        return ""

    candidates = [inner.get("vin"), inner.get("VIN")]
    for field in ("vehicle", "vehicleInfo", "data"):
        nested = inner.get(field)
        if isinstance(nested, dict):
            candidates.extend((nested.get("vin"), nested.get("VIN")))
    for candidate in candidates:
        value = str(candidate or "").strip().upper()
        if len(value) == 17 and value.isascii() and value.isalnum():
            return value
    return ""


def _bootstrap_entry(pair_dir: str) -> tuple[bool, str]:
    """Create the config entry through the config flow, without a human.

    This is the step that used to be manual, and it cannot go through
    ``xpeng.import_credentials``: on an install with no entry that service does not
    exist yet, because the integration's component-level setup only runs once an
    entry exists. Measured, not assumed (2026-10-11): with no entry, the xpeng
    service domain is absent from ``/api/services``, and starting a flow does not
    load it either. What *is* loaded on demand is the config flow, so driving it
    here creates the entry from the pair just minted - and creating the entry is
    what loads the component and registers the service for every renewal after it.
    """
    vin = _vin_from_pair(pair_dir)
    if not vin:
        return False, (
            "no VIN to create the config entry with, and no entry to import into: "
            "add the integration once by hand, or set this app's `vin` option"
        )

    status, body = _core_json("/config/config_entries/flow", {"handler": "xpeng"})
    if status != 200 or not isinstance(body, dict) or not body.get("flow_id"):
        return False, "could not start the config flow: %s %s" % (status, body)
    flow_id = body["flow_id"]

    # Which step this lands on is the integration's decision, not ours. Since 0.4.13 an
    # install with a pair already in `pair_dir` skips the method question and opens on
    # `import_tokens`; an older one opens on `user`, which is where the VIN and the auth
    # method belong. Sending `auth_method` to `import_tokens` is rejected by that step's
    # form schema:
    #
    #     400 {"errors":{"base":["not a valid option at 'auth_method'"]}}
    #
    # which is what happened on the first fresh install against 0.4.16: the pair was
    # minted, the entry was not created, and the install stayed half-finished with no
    # entities at all. So ask the step what it is, and answer only the question it asked.
    if body.get("step_id") == "user":
        status, body = _core_json(
            "/config/config_entries/flow/%s" % flow_id,
            {"vin": vin, "auth_method": "import"},
        )
        if status != 200 or not isinstance(body, dict):
            return False, "the import step was not reached: %s %s" % (status, body)

    if body.get("step_id") != "import_tokens":
        return False, "the import step was not reached: %s %s" % (status, body)

    status, body = _core_json(
        "/config/config_entries/flow/%s" % flow_id,
        {
            "tokens_path": os.path.join(pair_dir, "xpeng_tokens.json"),
            "tenant_path": os.path.join(pair_dir, "xpeng_tenant.json"),
            "vin": vin,
        },
    )
    if status != 200 or not isinstance(body, dict) or body.get("type") != "create_entry":
        return False, "the entry was not created: %s %s" % (status, body)
    _log("created the config entry from this pair (VIN %s)" % vin)
    return True, vin


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

        # A fresh install has no entry, and with no entry `import_credentials`
        # cannot run at all - the service is not registered until one exists. Create
        # it from the pair that was just minted, which is also what registers the
        # service, and treat that as the renewal: the flow stores this same pair.
        if _existing_entry() is None:
            bootstrapped, outcome = _bootstrap_entry(pair_dir)
            if not bootstrapped:
                return {
                    "ok": False,
                    "error": outcome,
                    "ms": int((time.time() - started) * 1000),
                }
            return {
                "ok": True,
                "bootstrapped": outcome,
                "onboarding": detail,
                "ms": int((time.time() - started) * 1000),
            }

        # ?return_response is mandatory, not a nicety: the integration registers
        # this service with SupportsResponse.ONLY, and Home Assistant refuses a
        # call that does not ask for the response with HTTP 400 "Service call
        # requires responses but caller did not ask for responses". The response
        # is also the import result - entry id, VIN, device id, client id.
        status, body = _call_service(
            "/services/xpeng/import_credentials?return_response",
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


def _no_entry_yet() -> bool:
    """Whether Home Assistant answered *and* has no XPENG entry.

    The distinction matters at startup: "no entry" is a fresh install that should be
    bootstrapped now, while "could not ask" is an API hiccup that must not set off a
    renewal - and `_existing_entry()` cannot tell them apart, because it answers None
    for both.
    """
    status, body = _core_json("/config/config_entries/entry", method="GET")
    if status != 200 or not isinstance(body, list):
        return False
    return not any(
        isinstance(entry, dict) and entry.get("domain") == "xpeng" for entry in body
    )


def _credentials_entity() -> str:
    """The entity id of the integration's credentials sensor, discovered once.

    ``binary_sensor.xpeng_<VIN suffix>_credentials_valid`` - the same suffix the entry
    title uses - so with a VIN in the pair it costs one small request. Without one,
    /states is scanned once and the answer kept.
    """
    if _CREDENTIALS_ENTITY:
        return _CREDENTIALS_ENTITY[0]
    vin = _vin_from_pair(PAIR_DIR)
    if vin:
        _CREDENTIALS_ENTITY.append(
            "binary_sensor.xpeng_%s_credentials_valid" % vin[-6:].lower()
        )
        return _CREDENTIALS_ENTITY[0]
    status, body = _core_json("/states", method="GET")
    if status == 200 and isinstance(body, list):
        for state in body:
            entity_id = str(state.get("entity_id") or "") if isinstance(state, dict) else ""
            if entity_id.startswith("binary_sensor.xpeng_") and entity_id.endswith(
                "_credentials_valid"
            ):
                _CREDENTIALS_ENTITY.append(entity_id)
                break
    return _CREDENTIALS_ENTITY[0] if _CREDENTIALS_ENTITY else ""


def _credentials_invalid() -> bool:
    """Whether Home Assistant says the credentials are already dead.

    It is the only thing that knows: to this app a session retired early and one
    simply due look identical. Polling it is what keeps the old automation's
    "renew immediately when the credentials go invalid" behaviour without needing
    that automation to exist.
    """
    entity_id = _credentials_entity()
    if not entity_id:
        return False
    status, body = _core_json("/states/%s" % entity_id, method="GET")
    if status == 200 and isinstance(body, dict):
        return body.get("state") == "off"
    # Stale id (renamed entity, or a different VIN): forget it and rediscover.
    _CREDENTIALS_ENTITY.clear()
    return False


def _renewal_due(now: float, next_run: float, credentials_invalid: bool) -> str:
    """Why a renewal should happen now, or "" if it should not.

    Split out because it is the whole of the scheduling policy and it is worth being
    able to test without a clock or a broker: the interval decides as before, and an
    already-invalid credential set overrides it, which is what stops a session that
    died early from waiting out the rest of the interval.
    """
    if now >= next_run:
        return "due"
    if credentials_invalid:
        return "credentials already invalid"
    return ""


def _scheduler(tick: float | None = None) -> None:
    """Renew on a timer, so nothing outside this app has to call it.

    The tick is short and the renewal interval long, so the time between renewals is
    spent checking whether the credentials have already gone. A session that dies
    early is therefore replaced within one tick instead of waiting out the rest of the
    interval - which is the one thing the Home Assistant automation did that a plain
    timer cannot.

    `renew_interval: 0` disables this, for an install that would rather trigger
    renewals itself (a manual call, or its own automation).
    """
    if RENEW_INTERVAL <= 0:
        _log("scheduler disabled (renew_interval=0): renewals must be triggered")
        return

    # A fresh install should not wait out a whole interval for its first renewal -
    # that wait is what made "trigger it once by hand" a documented step.
    if _no_entry_yet():
        _log("no config entry yet: renewing now to create it")
        _run_locked()

    STATE["next_run"] = time.time() + RENEW_INTERVAL
    # 10s floor so a misconfigured zero cannot become a hot loop. `tick` lets a test
    # drive the loop without waiting out a real interval.
    interval = max(10, INVALID_POLL_INTERVAL) if tick is None else tick
    _log(
        "scheduler: renew every %ds, credentials checked every %ds"
        % (RENEW_INTERVAL, interval)
    )
    while True:
        time.sleep(interval)
        reason = _renewal_due(time.time(), STATE["next_run"], _credentials_invalid())
        if not reason:
            continue
        result = _run_locked()
        if result.get("error") == "a renewal is already running":
            _log("scheduler: a renewal is already running, will retry")
            continue
        STATE["next_run"] = time.time() + RENEW_INTERVAL
        STATE["schedule_reason"] = reason
        _log(
            "scheduler: renewed (%s) ok=%s next in %ds"
            % (reason, result.get("ok"), RENEW_INTERVAL)
        )


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
            "scheduler": {
                "interval_s": RENEW_INTERVAL,
                "next_run_s": max(0, int(STATE["next_run"] - time.time()))
                if STATE["next_run"]
                else None,
                "last_reason": STATE["schedule_reason"] or None,
            },
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
    threading.Thread(target=_scheduler, name="scheduler", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
