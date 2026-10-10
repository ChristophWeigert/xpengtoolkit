#!/usr/bin/env python3
"""XPENG headless login + MQTT credential generator.

One-time login to get tokens, then call createMqttAccount.
Produces xpeng_tokens.json and xpeng_tenant.json for the HA integration.

Usage:
    python3 xpeng_login.py --email you@example.com --password yourpass

The script will:
  1. Fetch tenant data (teeId + key pool)
  2. Send email verification code
  3. Prompt you to enter the code from your email
  4. Login with your password
  5. Save tokens + tenant files
  6. Test createMqttAccount to verify everything works
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Allow running from this directory or from the repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from xpeng_api import (
    DEFAULT_APP_CONFIG,
    create_mqtt_account,
    fetch_tenant,
    load_tenant_cache,
    login_get_state,
    login_password,
    login_send_code,
    login_validate_code,
    mint_device_id,
    save_tenant_cache,
)


def resolve_device_id(explicit: str, tenant_path: str) -> tuple[str, str]:
    """The device id to sign as, and where it came from.

    The shipped default no longer pins one, so the REST layer raises rather than
    impersonate the author's device. A tenant on disk is bound to the id that
    fetched it — its key pool is sealed with md5(deviceId) as the AES key — so
    that id is adopted and the refreshed tenant stays usable. With no tenant to
    adopt, this is a first run and mints its own.
    """
    if explicit and explicit.strip():
        return explicit.strip(), "--device-id"
    try:
        tenant = load_tenant_cache(tenant_path)
    except Exception:
        tenant = None
    stored = str((tenant or {}).get("deviceId") or "").strip()
    if stored:
        return stored, f"adopted from {tenant_path}"
    return mint_device_id(), "minted (no device id in the tenant)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="XPENG headless login + MQTT credential generator"
    )
    parser.add_argument("--email", required=True, help="XPENG account email")
    parser.add_argument("--password", required=True, help="XPENG account password")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files (default: current dir)",
    )
    parser.add_argument(
        "--token-file",
        default="xpeng_tokens.json",
        help="Token output filename (default: xpeng_tokens.json)",
    )
    parser.add_argument(
        "--tenant-file",
        default="xpeng_tenant.json",
        help="Tenant cache filename (default: xpeng_tenant.json)",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip login, just test createMqttAccount with existing token file",
    )
    parser.add_argument(
        "--code",
        default="",
        help="Email verification code (skip interactive prompt)",
    )
    parser.add_argument(
        "--mqtt-test",
        action="store_true",
        default=True,
        help="Test createMqttAccount after login (default: True)",
    )
    parser.add_argument(
        "--no-mqtt-test",
        action="store_true",
        help="Skip createMqttAccount test",
    )
    parser.add_argument(
        "--device-id",
        default=None,
        help=(
            "32-hex device id to sign as. Default: the id the tenant file was "
            "minted under, else a freshly minted one — never a pinned device "
            "this install does not own."
        ),
    )
    args = parser.parse_args(argv)

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    token_path = os.path.join(output_dir, args.token_file)
    tenant_path = os.path.join(output_dir, args.tenant_file)

    config = dict(DEFAULT_APP_CONFIG)
    device_id, device_id_origin = resolve_device_id(args.device_id, tenant_path)
    config["ConfiguredDeviceId"] = device_id
    print(f"\nDevice id: {device_id}  ({device_id_origin})")

    if args.skip_login:
        return _test_mqtt(config, token_path, tenant_path)

    # ── Step 1: Fetch tenant ─────────────────────────────────
    print("\n[1/5] Fetching tenant data...")
    try:
        tenant = fetch_tenant(config)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1
    save_tenant_cache(tenant_path, tenant)
    print(f"  OK: teeId={tenant['teeId'][:16]}..., {len(tenant['_key_pool'])} keys")
    print(f"  Saved: {tenant_path}")

    # ── Step 2: Get user state ───────────────────────────────
    print(f"\n[2/5] Getting user state for {args.email}...")
    try:
        state_result = login_get_state(config, tenant, args.email)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1
    xid = state_result.get("xid", "")
    body = state_result.get("body_json", {})
    user_data = (body.get("data") or {})
    print(f"  OK: xid={xid[:20]}..." if xid else "  OK: (no xid)")
    if user_data:
        print(f"  User state: {json.dumps(user_data, indent=2)[:200]}")

    # ── Step 3: Send email code ──────────────────────────────
    print(f"\n[3/5] Sending verification code to {args.email}...")
    try:
        code_result = login_send_code(config, tenant, args.email, xid=xid)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1
    xid = code_result.get("xid") or xid
    code_body = code_result.get("body_json", {})
    print(f"  OK: code={code_body.get('code')}, msg={code_body.get('msg')}")

    # ── Step 4: Get code from user ───────────────────────────
    email_code = args.code
    if not email_code:
        print(f"\n  Check your email ({args.email}) for the verification code.")
        try:
            email_code = input("  Enter code: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 1
    if not email_code:
        print("  No code entered, aborting.")
        return 1

    # ── Step 4b: Validate code ───────────────────────────────
    print(f"\n[4/5] Validating code {email_code}...")
    try:
        validate_result = login_validate_code(
            config, tenant, args.email, email_code, xid=xid
        )
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1
    xid = validate_result.get("xid") or xid
    val_body = validate_result.get("body_json", {})
    print(f"  OK: code={val_body.get('code')}, msg={val_body.get('msg')}")

    # ── Step 5: Password login ───────────────────────────────
    print(f"\n[5/5] Logging in with password...")
    try:
        tokens = login_password(config, tenant, args.email, args.password, xid=xid)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1

    uid = tokens.get("uid")
    access = tokens.get("accessToken", "")
    refresh = tokens.get("refreshToken", "")
    print(f"  OK: uid={uid}")
    print(f"  accessToken: {access[:30]}..." if access else "  accessToken: (empty)")
    print(f"  refreshToken: {refresh[:30]}..." if refresh else "  refreshToken: (empty)")

    # Save tokens
    token_output = {
        "savedAt": int(time.time()),
        "email": args.email,
        "json": tokens,
    }
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump(token_output, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {token_path}")

    # ── Test createMqttAccount ───────────────────────────────
    if not args.no_mqtt_test:
        print("\n[+] Testing createMqttAccount...")
        return _test_mqtt(config, token_path, tenant_path)

    print("\nDone! Copy these files to your HA instance:")
    print(f"  scp {token_path} root@<your-ha-host>:/config/custom_components/xpeng/")
    print(f"  scp {tenant_path} root@<your-ha-host>:/config/custom_components/xpeng/")
    return 0


def _test_mqtt(
    config: dict, token_path: str, tenant_path: str
) -> int:
    """Test createMqttAccount with existing token + tenant files."""
    from xpeng_api import load_login_tokens

    try:
        tokens = load_login_tokens(token_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"  Cannot load tokens from {token_path}: {e}")
        return 1

    try:
        tenant = load_tenant_cache(tenant_path)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"  Cannot load tenant from {tenant_path}: {e}")
        print("  Fetching fresh tenant...")
        tenant = fetch_tenant(config)
        save_tenant_cache(tenant_path, tenant)

    print(f"  uid={tokens.get('uid')}, teeId={tenant.get('teeId', '?')[:16]}...")
    try:
        account = create_mqtt_account(config, tenant, tokens)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1

    print(f"\n  MQTT Account:")
    print(f"    clientId:  {account.get('clientId', '')}")
    print(f"    username:  {account.get('username', '')}")
    print(f"    password:  {account.get('password', '')[:20]}...")
    print(f"    serverSsl: {account.get('serverSsl', '')}")
    print(f"    expireTime: {account.get('expireTime', '')}")
    print(f"    expiresIn:  {account.get('expireInSec', '')}s")

    print(f"\nDone! Copy token + tenant files to HA:")
    print(f"  scp {token_path} root@<your-ha-host>:/config/custom_components/xpeng/")
    print(f"  scp {tenant_path} root@<your-ha-host>:/config/custom_components/xpeng/")
    print(f"\nThen set token_file in integration options to:")
    print(f"  /config/custom_components/xpeng/{os.path.basename(token_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
