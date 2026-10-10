"""XPENG login via "Sign in with Google" — the captcha-free onboarding path.

Why this exists
---------------

The password login (`/sso/app/v7/email/pwdLogin`) is the one step in the app
flow that enforces a NetEase Yidun slider captcha, and that captcha only
renders inside the app — so it cannot be answered headlessly. That blocks
onboarding for anyone but the person running the app.

The app also offers **Sign in with Google**. Verified live 2025-09-30:

    POST /sso/app/v7/google/authLogin {"clientId": <google client id>,
                                       "idToken": <bogus>}
      -> 12001227 "google id_token invalid"     <- validates the token with Google
      -> NO captcha code (12040106/12040101)     <- and no slider anywhere

So the whole problem reduces to obtaining a Google `id_token` for the app's
own OAuth client — and Google happily serves the app's sign-in page for it:

    GET https://accounts.google.com/o/oauth2/v2/auth
        ?client_id=<xpeng client id>
        &redirect_uri=com.googleusercontent.apps.<reversed>:/oauth2redirect
        &response_type=code&scope=openid email profile
        &code_challenge=…&code_challenge_method=S256
      -> HTTP 200, "Sign in - Google Accounts"

Onboarding for a non-technical user is then:

    1. open the auth URL in a browser, sign in with Google
    2. the browser redirects to a `com.googleusercontent.apps…` custom-scheme
       URL that "can't open" — but it carries the auth `code`
    3. paste that URL back here
    4. we exchange the code (PKCE) for an id_token
    5. POST the id_token to google/authLogin  ->  XPENG accessToken/refreshToken
    6. createMqttAccount                        ->  live telemetry

The honest caveat
-----------------

This drives **XPENG's own Google OAuth client** (`xpeng-open-auth`), not one of
ours. The user consents to XPENG's app with their own Google account and
receives access to their own vehicle — which is what Sign in with Google is
for. But it is worth stating plainly: the consent screen will say "xpeng-open-
auth", and a user should see that and understand it. If that reads as
impersonation to you, this is not the path for you. See DISTRIBUTION_PLAN.md
for the trade-offs; the alternatives are the captcha (blocked), the phone
bridge (not average), or the official export (historic).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any

# The app's own Google iOS OAuth client, from GoogleService-Info.plist in the
# installed app bundle (Wrapper/XPENG.app). Public by design — iOS clients have
# no client secret.
GOOGLE_CLIENT_ID = (
    "961737761970-j0quqbu51o3rp6m2e9sp8afqr5fnuje0.apps.googleusercontent.com"
)
GOOGLE_PROJECT = "xpeng-open-auth"

# The iOS redirect is the reversed client id as a custom URL scheme. Both of
# these render the sign-in page; use the canonical one.
REVERSED_SCHEME = (
    "com.googleusercontent.apps.961737761970-j0quqbu51o3rp6m2e9sp8afqr5fnuje0"
)
REDIRECT_URI = f"{REVERSED_SCHEME}:/oauth2redirect"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = "openid email profile"


class GoogleLoginError(RuntimeError):
    """An OAuth or authLogin step failed."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_auth_url() -> tuple[str, str]:
    """
    Return (url_to_open, code_verifier).

    Open `url_to_open` in a browser; after sign-in the browser lands on a
    `com.googleusercontent.apps…` URL carrying the code. Keep the verifier —
    the code cannot be exchanged without it.
    """
    verifier, challenge = _pkce_pair()
    params = urllib.parse.urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
        }
    )
    return f"{AUTH_URL}?{params}", verifier


def extract_code(text: str) -> str:
    """Pull the auth code out of whatever the user pastes back."""
    text = (text or "").strip()
    if not text:
        return ""
    query = urllib.parse.urlparse(text).query or text
    code = urllib.parse.parse_qs(query).get("code", [""])[0]
    if code:
        return urllib.parse.unquote(code)
    # Bare code pasted on its own: no spaces, plausible length, no '='.
    if " " not in text and len(text) >= 16 and "=" not in text:
        return text
    return ""


def exchange_code(code: str, verifier: str) -> str:
    """Exchange an authorization code for a Google `id_token` (PKCE, no secret)."""
    body = urllib.parse.urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise GoogleLoginError(f"token exchange HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        # Offline / DNS failure. Previously escaped as a bare OSError and turned
        # into HA's generic "unknown error" instead of a login failure.
        raise GoogleLoginError(f"token exchange could not reach Google: {exc.reason}") from exc
    except ValueError as exc:
        raise GoogleLoginError(f"token exchange returned non-JSON: {exc}") from exc

    if "error" in payload:
        raise GoogleLoginError(
            f"token exchange rejected: {payload.get('error')} — {payload.get('error_description')}"
        )
    token = payload.get("id_token")
    if not token:
        raise GoogleLoginError(f"no id_token in response: {sorted(payload)}")
    return token


def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Peek at the JWT claims (no verification — Google already issued it)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def tokeninfo(id_token: str) -> dict[str, Any]:
    """Ask Google itself to validate and decode the token. {} if it rejects."""
    url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# clientId candidates for google/authLogin. The first is the Google OAuth
# client id the token was issued for (the natural value); the others are fallbacks
# in case XPENG's backend keys validation off a different identifier.
CLIENT_ID_CANDIDATES = [
    GOOGLE_CLIENT_ID,
    "xp_app_norway_android",
    GOOGLE_CLIENT_ID.split(".")[0],  # numeric project number
]


def google_auth_login(
    api, config: dict | None, tenant: dict, id_token: str, client_id: str | None = None
) -> dict[str, Any]:
    """
    Trade a Google id_token for XPENG app tokens.

    `api` is the xpeng_api module. `config` may be None — every caller in the
    integration passes None, and the previous revision dereferenced it directly
    (`config.get(...)`), raising AttributeError and making the whole Google step
    surface a generic "unknown error". Resolve it through the api module's own
    defaults instead.

    Returns the tokens dict (accessToken, refreshToken, uid, …) on success;
    raises GoogleLoginError on any failure.
    """
    cfg = api._get_config(config)
    path = "/sso/app/v7/google/authLogin"
    url = api.absolute_url(cfg.get("LoginBaseUrl", ""), path)
    body = api.compact_json({"clientId": client_id or GOOGLE_CLIENT_ID, "idToken": id_token})
    headers, _ = api.build_xtee_signed_headers(cfg, tenant, url, "POST", body)
    resp = api._send_api_request(cfg, url, "POST", body, headers)
    if "error" in resp:
        raise GoogleLoginError(f"authLogin failed: {resp['error']}")

    status = int(resp.get("status", 0))
    if status >= 400:
        raise GoogleLoginError(f"authLogin HTTP {status}: {resp.get('body', '')[:200]}")

    try:
        parsed = json.loads(resp.get("body", "{}"))
    except ValueError as exc:
        raise GoogleLoginError(
            f"authLogin returned non-JSON: {resp.get('body', '')[:200]!r}"
        ) from exc

    code = parsed.get("code")
    data = parsed.get("data")
    if code not in (0, 200):
        desc = str(parsed.get("desc") or parsed.get("msg") or "").strip() or "unknown error"
        raise GoogleLoginError(f"google/authLogin rejected: code={code} {desc}")

    # Tokens may be plain JSON or an encrypted sData blob, like pwdLogin.
    tokens: Any = None
    key_pool = tenant.get("_key_pool") or {}
    if isinstance(data, dict) and data.get("sData"):
        try:
            plain = api.decrypt_xtee_blob_flexible(
                str(data["sData"]), key_pool
            ).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — surface as a login error, not a crash
            raise GoogleLoginError(f"could not decrypt authLogin sData: {exc}") from exc
        start = plain.find("{")
        if start < 0:
            raise GoogleLoginError("no JSON in decrypted authLogin sData")
        tokens = json.loads(plain[start:])
    elif isinstance(data, dict) and data.get("accessToken"):
        tokens = data
    elif isinstance(data, dict) and isinstance(data.get("json"), (dict, str)):
        inner = data["json"]
        tokens = json.loads(inner) if isinstance(inner, str) else inner
    elif isinstance(data, str) and data.lstrip().startswith("{"):
        tokens = json.loads(data)

    if not isinstance(tokens, dict) or not tokens.get("accessToken"):
        raise GoogleLoginError(f"authLogin returned no tokens: {str(data)[:200]!r}")

    tokens.setdefault("uid", "")
    return tokens
