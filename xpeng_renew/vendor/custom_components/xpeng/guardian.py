"""Mint a NetEase Yidun anti-bot token for XPENG's password login.

Why this exists
---------------

`POST /sso/app/v7/email/pwdLogin` is the **only** step in the XPENG app login
flow that enforces the captcha. `getUserStateByEmail`, the verification-code
send and the code validation all answer a clean `200` without one. Without a
token, `pwdLogin` answers `12040106`; with one it proceeds and reports an
ordinary password error if the password is wrong. Verified live 2025-09-30:

    no token        -> 12040106  captcha (NetEase Yidun, second code)
    Guardian token  -> 12001297  "Wrong password"      <- captcha cleared

The vendor is NetEase Yidun (易盾). Its "Guardian" SDK is the silent path — no
slider, no human, no interaction — and XPENG's own web client uses it. The
token comes back in about a second.

How it is obtained
------------------

By running NetEase's own JavaScript in a headless Chromium, because
reimplementing it means porting the `d`-field encryption out of a minified
bundle. That is the honest trade: a browser dependency now, in exchange for not
guessing at an obfuscated protocol. The wire format is recorded in
`tools/guardian_token.mjs` so the port stays on the table.

Two appIds work, and both were verified to clear `pwdLogin`:

  * `YD00907311582806` — the Guardian appId, default here. The same value is
    inside the captured HTProtect payload in `xpeng_api.py`.
  * `YD00458597415740` — the Android app's captcha product number.

Requirements
------------

`node` plus `playwright-core`, and a Chromium build. In the HA Add-on image
(scheduled) that means bundling Node and Chromium; for local testing, point
`XPENG_PLAYWRIGHT_DIR` at a directory with `node_modules/playwright-core`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_APP_ID = "YD00907311582806"
APP_ID_ANDROID = "YD00458597415740"

# Seconds. Chromium cold start dominates; the NetEase call itself is ~1s.
DEFAULT_TIMEOUT = 90

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "tools" / "guardian_token.mjs"


class GuardianError(RuntimeError):
    """The token could not be minted."""


def script_path() -> Path:
    """Absolute path to the Node helper."""
    return _SCRIPT


def available() -> tuple[bool, str]:
    """
    Whether a token can be minted here, and why not if not.

    Returned rather than raised so a caller can degrade gracefully — a
    deployment without Node should say so on a settings page, not fail at login.
    """
    if not _SCRIPT.exists():
        return False, f"guardian_token.mjs not found at {_SCRIPT}"
    if not shutil.which("node"):
        return False, "node is not on PATH"
    return True, "ok"


def mint_token(
    app_id: str = DEFAULT_APP_ID,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    node: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """
    Return a fresh anti-bot token, or raise `GuardianError`.

    The token is short-lived — mint it immediately before the login call rather
    than caching it.
    """
    ok, why = available()
    if not ok:
        raise GuardianError(why)

    environment = dict(os.environ)
    if env:
        environment.update(env)

    try:
        proc = subprocess.run(
            [node or "node", str(_SCRIPT), app_id],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise GuardianError(f"guardian token timed out after {timeout}s") from exc
    except OSError as exc:
        raise GuardianError(f"could not run node: {exc}") from exc

    # The helper prints one JSON line. Anything else on stdout is ignored so a
    # stray warning cannot break parsing.
    payload = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break

    if not isinstance(payload, dict):
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise GuardianError(f"no JSON from guardian helper (rc={proc.returncode}): {detail}")

    if not payload.get("ok") or not payload.get("token"):
        raise GuardianError(str(payload.get("error") or payload)[:300])

    return str(payload["token"])


def try_mint_token(app_id: str = DEFAULT_APP_ID, **kwargs) -> str | None:
    """`mint_token`, but None instead of raising. For callers that can carry on."""
    try:
        return mint_token(app_id, **kwargs)
    except GuardianError:
        return None
