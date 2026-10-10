#!/usr/bin/env python3
"""Interactive XPENG login: email+password, human-solved slider, Guardian token.

This is the *portable* path: it needs only the account's email and password and a
human dragging XPENG's own slider in a browser window. No app, no root, no device.

Flow (verified live 2026-10-06):
  1. fetch tenant
  2. getUserStateByEmail           -> xid, and typically 12040101 (slider)
  3. pwdLogin                      -> 12040106 "Slider captcha validation failed"
  4. solve_captcha.mjs --chrome    -> human drags the slider, prints `validate`
  5. POST /verification/app/v3/drag/validate {sceneType:LGN, captchaId, validate}
     with Xp-Xid = the session id  -> code 200 {"valid": true}   <-- the fix
  6. Guardian token, then pwdLogin again -> tokens

Usage:
    /usr/bin/python3 tools/solve_and_login.py --email a@b.c --password '...' [--save]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _find_component() -> Path:
    """Locate ``custom_components/xpeng`` in either supported layout.

    A full checkout puts it at ``ROOT/custom_components/xpeng``; a download of
    just ``tools/`` dropped inside the integration folder puts it at this
    file's parent.
    """
    here = Path(__file__).resolve().parent
    for cand in (ROOT / "custom_components" / "xpeng", here.parent, here / "xpeng"):
        if (cand / "xpeng_api.py").is_file() and (cand / "guardian.py").is_file():
            return cand
    return ROOT / "custom_components" / "xpeng"


COMP = _find_component()


def _find_tools_dir() -> Path:
    """Locate the directory holding the Node captcha helpers.

    Walks up from this file rather than hardcoding a checkout path, so the script
    works from any clone. Falls back to this file's own directory.
    """
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        for candidate in (base / "tools", base):
            if (candidate / "solve_captcha.mjs").is_file():
                return candidate
    return here


SCRIPT_DIR = _find_tools_dir()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_json(cmd: list[str], env: dict, timeout: int = 400) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "err": ((proc.stderr or "") + (proc.stdout or ""))[-400:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--save", action="store_true", help="write xpeng_tokens.json + xpeng_tenant.json")
    ap.add_argument("--guardian-app-id", default=None)
    ap.add_argument("--playwright-dir", default="/tmp/pwdir")
    ap.add_argument("--headless", action="store_true", help="do NOT show the slider (will likely fail)")
    args = ap.parse_args()

    env = dict(os.environ)
    env["XPENG_PLAYWRIGHT_DIR"] = args.playwright_dir

    api = load("xpeng_api", COMP / "xpeng_api.py")
    guardian = load("guardian", COMP / "guardian.py")
    cfg = dict(api.DEFAULT_APP_CONFIG)
    app_id = args.guardian_app_id or guardian.DEFAULT_APP_ID

    tenant = api.fetch_tenant(cfg)
    print(f"[*] tenant teeId={str(tenant.get('teeId'))[:20]} keys={len(tenant['_key_pool'])}")

    state = api.login_get_state(cfg, tenant, args.email)
    xid = state.get("xid") or ""
    print(f"[*] getState code={state['body_json'].get('code')} xid={xid[:36]}")

    body = api.compact_gson_json(
        {"email": args.email, "password": api.rsa_encrypt_password(args.password)}
    )
    attempt = api._login_signed_step(
        cfg, tenant, "pwdLogin", "/sso/app/v7/email/pwdLogin", body, xid=xid
    )
    code = attempt["body_json"].get("code")
    print(f"[*] pwdLogin code={code} {str(attempt['body_json'].get('desc') or '')[:60]}")
    xid = attempt.get("xid") or xid

    if code not in (0, 200):
        if 12040101 not in (code,) and code not in (12040106,):
            print("[!] unexpected gate; stopping")
            return 1
        print("[*] opening the slider browser - DRAG IT")
        solver_cmd = ["node", str(SCRIPT_DIR / "solve_captcha.mjs"), "--timeout", "300"]
        if not args.headless:
            solver_cmd.append("--chrome")
        else:
            solver_cmd.append("--headless")
        sol = run_json(solver_cmd, env)
        if not (sol.get("ok") and sol.get("validate")):
            print(f"[!] captcha not solved: {str(sol)[:200]}")
            return 1
        print(f"[*] validate {len(sol['validate'])} chars, captchaId={sol.get('captchaId')}")

        vbody = api.compact_json({
            "sceneType": "LGN",
            "captchaId": sol.get("captchaId"),
            "validate": sol["validate"],
        })
        vurl = api.absolute_url(cfg.get("LoginBaseUrl", ""), "/verification/app/v3/drag/validate")
        vheaders, _ = api.build_xtee_signed_headers(cfg, tenant, vurl, "POST", vbody, xid=xid)
        vresp = api._send_api_request(cfg, vurl, "POST", vbody, vheaders)
        vparsed = json.loads(vresp.get("body", "{}"))
        print(f"[*] verify captcha code={vparsed.get('code')} data={str(vparsed.get('data'))[:60]}")
        if vparsed.get("code") not in (0, 200):
            print("[!] the server refused the solved captcha; stopping")
            return 1
        xid = api._extract_xid(vresp) or xid

        gt = run_json(
            ["node", str(SCRIPT_DIR / "guardian_token.mjs"), app_id, "--headful"],
            env, timeout=300,
        )
        if not gt.get("ok"):
            print(f"[!] guardian failed: {str(gt)[:160]}")
            return 1
        print("[*] fresh Guardian token minted")

        tokens = api.login_password(
            cfg, tenant, args.email, args.password, xid=xid,
            ptoken=gt.get("token"), auto_ptoken=False,
        )
    else:
        data = attempt["body_json"].get("data")
        tokens = data

    if not isinstance(tokens, dict) or not tokens.get("accessToken"):
        print(f"[!] no tokens: {str(tokens)[:200]}")
        return 1

    print(f"[+] LOGIN OK uid={tokens.get('uid')} accessToken={str(tokens['accessToken'])[:28]}...")
    print(f"[+] refreshToken={str(tokens.get('refreshToken'))[:28]}...")

    if args.save:
        tokens_path = COMP / "xpeng_tokens.json"
        tenant_path = COMP / "xpeng_tenant.json"
        tokens_path.write_text(json.dumps(tokens, indent=2))
        tenant_path.write_text(json.dumps(
            {k: v for k, v in tenant.items() if k != "_key_pool"}, indent=2
        ))
        print(f"[+] wrote {tokens_path}")
        print(f"[+] wrote {tenant_path}")

        mqtt = api.create_mqtt_account(cfg, tenant, tokens)
        print(f"[+] createMqttAccount: {json.dumps(mqtt, ensure_ascii=False)[:300]}")
    else:
        Path("/tmp/xpeng_login_tokens.json").write_text(json.dumps(tokens, indent=2))
        print("[*] tokens saved to /tmp/xpeng_login_tokens.json (pass --save to persist into the component)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
