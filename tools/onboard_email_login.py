#!/usr/bin/env python3
"""Portable XPENG onboarding with email + password. NO app, NO root, NO device.

The only human step is dragging XPENG's own slider captcha in a browser window,
unless a hosted solver is used (--captcha-solver), in which case the whole run is
unattended -
exactly what the account owner does in the app. Everything else is HTTP.

Flow (all steps verified live against a real account):
  1. fetch tenant
  2. POST /sso/app/v7/email/getUserStateByEmail  -> xid (+ 12040101 slider gate)
  3. POST /sso/app/v7/email/pwdLogin             -> 12040106 "Slider captcha validation failed"
  4. solve_captcha.mjs --chrome                  -> human drags slider -> `validate`
  5. POST /verification/app/v3/drag/validate {sceneType:LGN,captchaId,validate}
       with Xp-Xid = the step-2 session id   -> 200 {"valid": true}    <-- required
  6. Guardian token, then pwdLogin again         -> uid + accessToken + refreshToken
  7. save xpeng_tokens.json AND xpeng_tenant.json together (same tenant!)
  8. POST /biz/v5/mqtt/createMqttAccount/app     -> MQTT clientId + password

Usage:
    /usr/bin/python3 tools/onboard_email_login.py --email a@b.c --password '...' --save
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parent.parent          # repo root, or the integration dir


def _find_component() -> Path:
    """Locate ``custom_components/xpeng``.

    Looks in the two layouts people actually end up with:

    * a full repo checkout, where this file is ``<root>/tools/xpeng_tools.py``
      and the component is ``<root>/custom_components/xpeng``;
    * a download of just the ``tools/`` directory dropped inside the
      integration folder, i.e. ``custom_components/xpeng/tools/`` - here the
      component is this file's own parent.

    Falls back to ``ROOT/custom_components/xpeng`` so the error message names a
    real path rather than an invented one.
    """
    here = Path(__file__).resolve().parent
    candidates = (
        ROOT / "custom_components" / "xpeng",   # full checkout
        here.parent,                            # tools/ inside the component
        here / "xpeng",                         # tools/xpeng
        ROOT / "xpeng",
    )
    for cand in candidates:
        if (cand / "xpeng_api.py").is_file() and (cand / "guardian.py").is_file():
            return cand
    return ROOT / "custom_components" / "xpeng"


COMP = _find_component()

#: Home Assistant's own copy of this component, when there is one. HACS replaces
#: that whole directory on update or reinstall, so anything written inside it -
#: live credentials included - does not survive, which is what turned "update the
#: integration" into "onboard the car again" (reported 2026-10-13).
_LIVE_COMPONENT: Final = Path("/config/custom_components/xpeng")

#: Where the renewal app publishes the pair, and what the import form looks in
#: first. Durable, and already the documented home for these two files.
_DURABLE_PAIR_DIR: Final = Path("/config/xpeng-secrets")


def _save_dir(override: str) -> tuple[Path, str]:
    """Where ``--save`` writes the pair, and a phrase saying why.

    Next to the component is right for the documented desktop flow: unzip the
    repository somewhere, this tool writes the two files beside the copy it just
    used, and their contents get pasted into Home Assistant. Nothing here
    outlives a HACS update in that flow because nothing here *is* the installed
    integration.

    It is the wrong place when the component is Home Assistant's own: HACS
    replaces that directory, so the pair is gone by the next update - and the
    import form then asks for a pair the user already had. In that one layout
    the pair goes to the durable directory instead, which is also where the
    renewal app puts its own. ``--out-dir`` overrides both.
    """
    if override:
        return Path(override).expanduser(), "from --out-dir"
    if COMP.resolve() == _LIVE_COMPONENT.resolve():
        return _DURABLE_PAIR_DIR, (
            "Home Assistant's own integration directory, which HACS replaces on "
            "every update (use --out-dir to choose somewhere else)"
        )
    return COMP, "next to the integration files this run used"

#: The captcha / anti-bot helpers are Node scripts that live in the repository's
#: top-level tools/ directory. This script is shipped from xpeng-google/tools/,
#: so that directory is located relative to this file instead of being
#: hard-coded to the path it happened to be written at.
CAPTCHA_HELPERS: Final = ("solve_captcha.mjs", "guardian_token.mjs")


def find_tools_dir() -> Path:
    """The directory holding the Node captcha helpers.

    Walks up from this file looking for ``solve_captcha.mjs`` and
    ``guardian_token.mjs`` — true both in the repo checkout (where the helpers
    sit in the top-level tools/, a parent of xpeng-google/tools/) and when this
    script is run from a checkout's own tools/ directory. Falls back to this
    file's directory so any failure names a real path.
    """
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        for candidate in (base / "tools", base):
            if all((candidate / name).is_file() for name in CAPTCHA_HELPERS):
                return candidate
    return here


SCRIPT_DIR = find_tools_dir()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _first_vin(payload) -> str:
    """The first VIN in a myVehicles response, or "".

    The endpoint answers either a bare list of vehicles or an object wrapping one
    (``vehicles``/``list``), and the field is ``vin`` in both shapes - the same two
    the integration handles when it reads this response. A VIN is only accepted in
    the shape the rest of the code expects (17 alphanumeric ASCII characters), so a
    placeholder or a partial record cannot end up on the config entry.
    """
    if isinstance(payload, dict):
        payload = payload.get("vehicles") or payload.get("list") or []
    if not isinstance(payload, list):
        return ""
    for vehicle in payload:
        if not isinstance(vehicle, dict):
            continue
        vin = str(vehicle.get("vin") or "").strip().upper()
        if len(vin) == 17 and vin.isascii() and vin.isalnum():
            return vin
    return ""


def summarise_output(text: str, limit: int = 1600) -> str:
    """Head and tail of a failed helper's output.

    Neither end alone is enough. The exception is the first line, and the
    explanation is often the last (Playwright prints its "run npx playwright
    install" box *after* the line naming the missing executable), so a plain
    tail kept only the box and threw away the cause - which is exactly what made
    a missing-Chromium failure unreadable inside the renewal add-on.

    When even head-and-tail has to be cut, cut the MIDDLE: a helper that fails
    this way reports one very long line, and both its ends carry information.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) > 6:
        lines = lines[:3] + ["..."] + lines[-3:]
    joined = " | ".join(lines)
    if len(joined) <= limit:
        return joined
    half = (limit - 5) // 2
    return "%s ... %s" % (joined[:half], joined[-half:])


def run_json(cmd, env, timeout=400):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "err": summarise_output((proc.stderr or "") + (proc.stdout or ""))}


def solve_via_service(url: str, token: str = "", timeout: int = 120) -> dict:
    """Ask a hosted slider solver for a `validate` string.

    Same contract as the local browser step (solve_captcha.mjs): a JSON object with
    ok/validate/captchaId, except nobody has to drag anything. See the
    xpeng-slider-solver project for the service.
    """
    if "/app/" in url or "/api/hassio_ingress/" in url:
        # The sidebar panel, not the API. Home Assistant serves that route as a
        # GET-only ingress page, so POSTing to it answers `405 Method Not Allowed`,
        # which reads as a broken solver rather than a wrong address - and it
        # resolves, so it does not look like a typo either. A live install lost
        # three days to this (2026-10-13); /solve lives on the solver's own port.
        return {
            "ok": False,
            "error": (
                f"{url} is the solver's ingress panel, which only answers GET. Use "
                "the solver's own address instead: http://<its container hostname>:"
                "8080 (hyphens, not underscores) or http://<your-ha-host>:8080."
            ),
        }
    request = urllib.request.Request(
        url.rstrip("/") + "/solve",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "error": f"solver HTTP {exc.code}: {body}"}
    except Exception as exc:  # noqa: BLE001 - report, never traceback at the user
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    # Deliberately not required: the tool asks for it when it is missing. That keeps
    # a password out of the documentation's example commands and out of shell
    # history, which is where a --password '...' argument ends up.
    ap.add_argument("--password", default="")
    ap.add_argument("--save", action="store_true",
                    help="write xpeng_tokens.json + xpeng_tenant.json "
                         "(where: see --out-dir)")
    ap.add_argument(
        "--out-dir",
        default="",
        help="where --save writes the pair (default: next to the integration files, "
             "or /config/xpeng-secrets when those files are Home Assistant's own "
             "HACS-managed copy, which an update would wipe)",
    )
    ap.add_argument("--mqtt", action="store_true", help="also call createMqttAccount")
    ap.add_argument("--playwright-dir", default="/tmp/pwdir")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--guardian-app-id", default=None)
    ap.add_argument(
        "--tools-dir",
        default=os.environ.get("XPENG_TOOLS_DIR", ""),
        help="directory holding solve_captcha.mjs and guardian_token.mjs "
             "(default: found by walking up from this script; "
             "XPENG_TOOLS_DIR overrides)",
    )
    ap.add_argument(
        "--device-id",
        default=None,
        help="32-hex device id. Omit to generate a fresh one, which is what a new "
             "user should get: two installs sharing an id impersonate each other.",
    )
    ap.add_argument(
        "--captcha-solver",
        default=os.environ.get("XPENG_CAPTCHA_SOLVER", ""),
        help="base URL of a hosted slider solver (e.g. http://host:8080). When "
             "set, the captcha is solved over HTTP and no browser opens here; "
             "otherwise solve_captcha.mjs opens one for a human drag. "
             "XPENG_CAPTCHA_SOLVER overrides.",
    )
    ap.add_argument(
        "--captcha-solver-token",
        default=os.environ.get("XPENG_CAPTCHA_SOLVER_TOKEN", ""),
        help="bearer token for that solver, if it requires one. "
             "XPENG_CAPTCHA_SOLVER_TOKEN overrides.",
    )
    ap.add_argument(
        "--device-id-out",
        default="/tmp/xpeng_device_id.txt",
        help="where to record the device id for the HA config entry",
    )
    ap.add_argument(
        "--guardian-headless",
        action="store_true",
        help="run the Guardian helper without a browser window. Required wherever "
             "there is no display - a server or the renewal add-on - because a "
             "headed launch fails outright with 'launched a headed browser "
             "without having a XServer running'. The default stays headful so an "
             "interactive run can still drag the slider NetEase falls back to.",
    )
    args = ap.parse_args()

    if not args.password:
        try:
            args.password = getpass.getpass("XPENG account password: ")
        except (EOFError, KeyboardInterrupt):
            print("[-] no password given", file=sys.stderr)
            return 2
        if not args.password:
            print("[-] no password given", file=sys.stderr)
            return 2

    env = dict(os.environ)
    env["XPENG_PLAYWRIGHT_DIR"] = args.playwright_dir
    script_dir = Path(args.tools_dir).expanduser() if args.tools_dir else SCRIPT_DIR
    solve_js = script_dir / "solve_captcha.mjs"
    guardian_js = script_dir / "guardian_token.mjs"

    # Fail with something actionable rather than a bare ImportError: copying
    # tools/ out on its own is the most common wrong-layout mistake.
    missing = [
        name
        for name in ("xpeng_api.py", "guardian.py", "const.py", "xpeng_errors.py")
        if not (COMP / name).is_file()
    ]
    if missing:
        raise SystemExit(
            "cannot find the integration's Python files next to this tool.\n"
            f"  looked in: {COMP}\n"
            f"  missing  : {', '.join(missing)}\n"
            "This tool imports them from the integration, so unzip the WHOLE\n"
            "repository and keep the layout intact:\n"
            "  <root>/tools/onboard_email_login.py\n"
            "  <root>/custom_components/xpeng/xpeng_api.py\n"
            "or place this tools/ directory inside custom_components/xpeng/."
        )

    api = load("xpeng_api", COMP / "xpeng_api.py")
    guardian = load("guardian", COMP / "guardian.py")
    cfg = dict(api.DEFAULT_APP_CONFIG)
    app_id = args.guardian_app_id or guardian.DEFAULT_APP_ID

    # Each install must sign as its OWN device. The id goes into the XTEE
    # authorization, the User-Agent, and (via createMqttAccount) the MQTT
    # clientId, so it has to be chosen before anything is fetched or signed.
    device_id = (args.device_id or secrets.token_hex(16)).lower()
    cfg["ConfiguredDeviceId"] = device_id
    print(f"[0] device id = {device_id}")

    # 1. tenant
    tenant = api.fetch_tenant(cfg)
    print(f"[1] tenant teeId={str(tenant.get('teeId'))[:20]} keys={len(tenant['_key_pool'])}")

    # 2. state -> xid
    state = api.login_get_state(cfg, tenant, args.email)
    xid = state.get("xid") or ""
    print(f"[2] getState code={state['body_json'].get('code')} xid={xid[:36]}")

    def pwd_login(ptoken=None):
        body = api.compact_gson_json(
            {"email": args.email, "password": api.rsa_encrypt_password(args.password)}
        )
        return api._login_signed_step(
            cfg, tenant, "pwdLogin", "/sso/app/v7/email/pwdLogin", body,
            xid=xid, ptoken=ptoken,
        )

    # 3. first attempt
    attempt = pwd_login()
    code = attempt["body_json"].get("code")
    print(f"[3] pwdLogin code={code} {str(attempt['body_json'].get('desc') or '')[:60]}")
    xid = attempt.get("xid") or xid
    tokens = None

    if code in (0, 200):
        tokens = attempt["body_json"].get("data")
    else:
        # 4. clear the slider, then bind the solve to this session
        if args.captcha_solver:
            if not guardian_js.is_file():
                print(f"[!] {guardian_js.name} is missing from {script_dir}: the "
                      "guardian token is still needed on this path (only the "
                      "slider moves to the service).")
                return 1
            print(f"[4] asking the slider solver at {args.captcha_solver}")
            sol = solve_via_service(args.captcha_solver, args.captcha_solver_token)
            if not (sol.get("ok") and sol.get("validate")):
                print(f"[!] solver produced no validate: {str(sol)[:200]}")
                return 1
            print(f"[4] solved by the service: {sol.get('attempts')} attempt(s), "
                  f"{sol.get('ms')}ms")
        else:
            missing = [p.name for p in (solve_js, guardian_js) if not p.is_file()]
            if missing:
                print(f"[!] captcha helper(s) missing from {script_dir}: "
                      f"{', '.join(missing)}")
                print("[!] they live in the repository's top-level tools/ directory; "
                      "run this from a repo checkout, or pass --tools-dir / set "
                      "XPENG_TOOLS_DIR to point at it. A hosted solver "
                      "(--captcha-solver) needs only guardian_token.mjs.")
                return 1
            print("[4] opening slider browser - DRAG THE SLIDER TO THE RIGHT")
            cmd = ["node", str(solve_js), "--timeout", "300"]
            cmd += ["--headless"] if args.headless else ["--chrome"]
            sol = run_json(cmd, env)
            if not (sol.get("ok") and sol.get("validate")):
                print(f"[!] captcha not solved: {str(sol)[:200]}")
                return 1
        print(f"[5] validate={len(sol['validate'])} chars captchaId={sol.get('captchaId')}")

        vbody = api.compact_json({
            "sceneType": "LGN",
            "captchaId": sol.get("captchaId"),
            "validate": sol["validate"],
        })
        vurl = api.absolute_url(cfg.get("LoginBaseUrl", ""), "/verification/app/v3/drag/validate")
        vheaders, _ = api.build_xtee_signed_headers(cfg, tenant, vurl, "POST", vbody, xid=xid)
        vresp = api._send_api_request(cfg, vurl, "POST", vbody, vheaders)
        vparsed = json.loads(vresp.get("body", "{}"))
        print(f"[5] drag/validate code={vparsed.get('code')} data={str(vparsed.get('data'))[:60]}")
        if vparsed.get("code") not in (0, 200):
            print("[!] server refused the solved captcha")
            return 1
        xid = api._extract_xid(vresp) or xid

        # 6. Guardian token + retry
        guardian_cmd = ["node", str(guardian_js), app_id]
        if not args.guardian_headless:
            # Headful lets a human drag the slider when NetEase challenges the
            # silent path, but it needs a display: on a server or in the renewal
            # add-on the launch dies with "launched a headed browser without
            # having a XServer running", which is what stopped every unattended
            # renewal after the captcha had already been solved.
            guardian_cmd.append("--headful")
        gt = run_json(guardian_cmd, env, timeout=300)
        if not gt.get("ok"):
            # Not truncated here: the browser's own account of why it refused to
            # start is long, and cutting it at 160 characters is what hid the
            # cause of a container launch failure for several rounds. The helper
            # caps its own message; the run log keeps whatever arrives.
            print(f"[!] guardian failed: {str(gt)}")
            return 1
        print("[6] Guardian token minted; retrying pwdLogin")
        try:
            tokens = api.login_password(
                cfg, tenant, args.email, args.password, xid=xid,
                ptoken=gt.get("token"), auto_ptoken=False,
            )
        except Exception as exc:
            print(f"[!] login still refused: {type(exc).__name__}: {str(exc)[:200]}")
            return 1

    if not isinstance(tokens, dict) or not tokens.get("accessToken"):
        print(f"[!] no tokens: {str(tokens)[:200]}")
        return 1
    # Never echo the token itself, not even a prefix: this output is what gets
    # pasted into issues and chat logs. A length is enough to eyeball a response.
    print(f"[+] LOGIN OK uid={tokens.get('uid')} "
          f"accessToken=<{len(str(tokens['accessToken']))} chars>")

    # 7. persist BOTH, from the SAME tenant
    if args.save:
        # Record which account these tokens belong to. The token itself cannot
        # answer this: it is a tenant-sealed blob whose plaintext is only the
        # teeId and this install's deviceId, and the tenant holds keys, not
        # account data. The email is already in hand - the user just typed it -
        # and would otherwise be lost, leaving "which account is this?" to be
        # answered by whoever wrote the file down. Carries into the entry data
        # via the import step, which passes unknown keys through unchanged.
        tokens["account_email"] = args.email
        # Record the VIN for the same reason, and one step further: it is the one
        # piece of identity the pair cannot carry, and without it nothing can
        # create the config entry unattended. The import flow requires a VIN, and
        # it asks for one only because most pairs have none - so asking the API
        # once here means the form can leave it blank and a first renewal can
        # create the entry with no human at all.
        #
        # Best effort: a login that worked is worth saving even if this call does
        # not, in which case the form asks for the VIN as it always did.
        try:
            vin = _first_vin(api.get_vehicles(None, tenant, tokens))
        except Exception as exc:  # noqa: BLE001 - never lose a good login to this
            vin = ""
            print(f"[7] could not read the VIN ({type(exc).__name__}: {exc})")
        if vin:
            tokens["vin"] = vin
            print(f"[7] VIN {vin}")
        else:
            print("[7] no VIN in the vehicle list - the import form will ask for one")
        # The integration's load_login_tokens() requires a wrapper object with the
        # tokens under "json" (the capturing tools emit that shape). Writing the
        # tokens flat silently produces a file HA cannot read at all - the loader
        # raises "No JSON token object found". Always write the wrapper.
        wrapper = {
            "json": tokens,
            "jsonText": json.dumps(tokens, separators=(",", ":")),
        }
        save_dir, why = _save_dir(args.out_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / "xpeng_tokens.json").write_text(json.dumps(wrapper, indent=2))
        api.save_tenant_cache(str(save_dir / "xpeng_tenant.json"), tenant)
        # these are live account credentials
        os.chmod(save_dir / "xpeng_tokens.json", 0o600)
        os.chmod(save_dir / "xpeng_tenant.json", 0o600)
        print(f"[7] pair directory: {save_dir} ({why})")
        print(f"[7] saved {save_dir/'xpeng_tokens.json'} (wrapper: json + jsonText)")
        print(f"[7] saved {save_dir/'xpeng_tenant.json'} (teeId={str(tenant.get('teeId'))[:20]})")
        Path(args.device_id_out).write_text(device_id)
        print(f"[7] device id -> {args.device_id_out}")
    else:
        Path("/tmp/xpeng_login_tokens.json").write_text(json.dumps(tokens, indent=2))
        api.save_tenant_cache("/tmp/xpeng_login_tenant.json", tenant)
        print("[7] saved tokens+tenant to /tmp (no --save)")

    # 8. MQTT credentials.
    #
    # Deliberately uses the SAME in-memory `tenant` that performed the login -
    # never a reloaded cache. Reloading is the documented trap: load_tenant_cache()
    # only re-fetches when the file is missing or invalid, so a stale-but-parseable
    # tenant loads silently and every later call fails `12010104 Login status
    # expired`, which is indistinguishable from actually-expired tokens.
    if args.mqtt:
        if not args.save:
            print("[8] --mqtt requires --save (the tenant must be the one written "
                  "next to the tokens); skipping")
            return 0
        try:
            mqtt = api.create_mqtt_account(cfg, tenant, tokens)
            print(f"[8] createMqttAccount OK clientId={str(mqtt.get('clientId'))[:34]} "
                  f"password=<{len(str(mqtt.get('password') or ''))} chars> "
                  f"server={mqtt.get('serverSsl')}")
            print(f"[8] device id is in clientId: {device_id in str(mqtt.get('clientId'))}")
        except Exception as exc:
            print(f"[8] createMqttAccount FAILED: {type(exc).__name__}: {str(exc)[:200]}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
