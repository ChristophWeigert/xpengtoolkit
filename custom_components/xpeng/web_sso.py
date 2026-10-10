"""XPENG web SSO — browser-assisted login, without Playwright.

This is the **fallback** login route. The primary route is the app API
(`xpeng_api.py`: email + emailed code + password against
`apps-global.xiaopeng.com`), which is what mints MQTT broker credentials.

This module drives the *web* SSO instead, and its whole reason for existing is
that it needs no password handling at all: the user signs in with their own
browser — password manager, captcha, whatever — and hands back a code.

How the exchange works (verified live 2025-09-30)
-------------------------------------------------

The exchange is **server-side**, not JavaScript. `main.js` on
`iot-web-eur.xpeng.com` contains no `webAuthSdk`, no `getToken`, no
`exchange`, no `oauth2`, no `accessToken` — none of it. The callback path is
answered by `openresty` / `xp-vmp-client-gateway-europe-boot` *before* the SPA
ever loads, and redeeming a code is one unauthenticated GET:

    GET <callbackUrl>&code=<code>
      -> 200 {"code":4001002,  "msg":"Lack param"}          no code
      -> 200 {"code":12001202, ...}                          bad/expired code
      -> 200 {"code":12150004, "msg":"...state校验失败"}       state mismatch
      -> 405 {"code":405}                                    POST is rejected
      -> on success: Set-Cookie
             wasxid-<appId>=<32 hex>   (HttpOnly)
             wasxuid=<uid>             (HttpOnly)
             wasxuid_r=<uid>           (JS-readable)

Two details worth knowing:

  * **The two states differ.** `authUrl` carries one `state` and `callbackUrl`
    carries a *different* one. The callback validates its own, so redeem
    against the callback URL verbatim — do not substitute the auth state.
  * **The state is single-use and short-lived.** Redeem immediately.

What this session can and cannot do
-----------------------------------

The cookies authorise the **web** gateway (`iot-web-eur.xpeng.com`). They are
*not* an app-API `accessToken`/`refreshToken`, so this route **cannot** call
`createMqttAccount` and **cannot** reach the MQTT broker. It is for reading
data over the web API — and as a diagnostic when the app-API login misbehaves.

For MQTT credentials you need the app API route. See `xpeng_api.py`.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

WEB_BASE = "https://iot-web-eur.xpeng.com"
APP_ID = "xp_car_owner_service_oversea"

# Where a successful callback should land. The energy-report page is what the
# Android/web client itself uses; anything under the SPA works.
DEFAULT_REDIRECT = f"{WEB_BASE}/car-service/energy_report"

CALLBACK_PATH = "/car-service/energy_report/webAuthSdk/callback"
VEHICLE_LIST_PATH = "/api/energy/user/vehicle/list/all"

# Codes the callback returns in a 200 body. Branched on so callers get a
# sentence rather than a number.
CODE_LACK_PARAM = 4001002
CODE_BAD_CODE = 12001202
CODE_BAD_STATE = 12150004

ERROR_MESSAGES = {
    CODE_LACK_PARAM: "the callback was called without a code",
    CODE_BAD_CODE: "the authorisation code was rejected (expired, or already used)",
    CODE_BAD_STATE: "the state did not match — redeem against the callbackUrl verbatim",
}


class WebSsoError(RuntimeError):
    """A login-URL request or a code redemption failed."""


def _device_id() -> str:
    """A 16-char id, mirroring what the SPA mints into its `<appId>` cookie."""
    return secrets.token_hex(8)


def _wire_headers(uid: str = "-1", device_id: str = "") -> dict[str, str]:
    """
    The header set the web gateway accepts.

    Note there is no `XP-Signature` and no `XP-Authorization` here — this is a
    different gateway from the app API and does not use the XTEE V2 signing.
    `XP-DeviceId` is a plain client-chosen string; the SPA itself defaults it
    to whatever is in the `<appId>` cookie, and the checked-in client used the
    literal `web-test-001` for months without complaint.
    """
    return {
        "XP-Appid": APP_ID,
        "XP-Uid": uid,
        "XP-DeviceId": device_id or _device_id(),
        "XP-Xid": "",
        "XP-Nonce": str(int(time.time() * 1000)),
        "Content-Type": "application/json",
    }


@dataclass
class LoginUrls:
    """The pair a caller needs to start a browser login."""

    auth_url: str
    callback_url: str

    @property
    def state(self) -> str:
        """The callback's own state — the one the redemption validates."""
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.callback_url).query).get(
            "state", [""]
        )[0]


@dataclass
class WebSession:
    """A redeemed web session: the cookie triple, plus a little plumbing."""

    uid: str
    cookies: dict[str, str] = field(default_factory=dict)
    device_id: str = ""

    def describe(self) -> str:
        masked = {
            k: (v[:8] + "…" if len(v) > 10 else v) for k, v in sorted(self.cookies.items())
        }
        return f"uid={self.uid} cookies={masked}"

    def _opener(self) -> urllib.request.OpenerDirector:
        jar = http.cookiejar.CookieJar()
        for name, value in self.cookies.items():
            jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0, name=name, value=value, port=None, port_specified=False,
                    domain=".iot-web-eur.xpeng.com", domain_specified=True, domain_initial_dot=True,
                    path="/", path_specified=True, secure=True, expires=None,
                    discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
                )
            )
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(self, path: str, *, method: str = "GET", body: dict | None = None) -> Any:
        """Call a web API path with this session's cookies."""
        url = path if path.startswith("http") else f"{WEB_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = _wire_headers(uid=self.uid, device_id=self.device_id)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener().open(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise WebSsoError(f"HTTP {exc.code} from {path}: {exc.read()[:200]!r}") from exc

    def vehicles(self) -> Any:
        """The vehicle list — the cheap call that proves the session works."""
        return self.request(VEHICLE_LIST_PATH, method="POST", body={"preUid": self.uid})


def request_login_urls(redirect_url: str = DEFAULT_REDIRECT) -> LoginUrls:
    """
    Ask for a fresh authUrl / callbackUrl pair.

    Unauthenticated. The response also carries `code: 12150008` ("session
    validation failed") on the envelope, which is expected and harmless — the
    pair is still issued. Only the `data` block matters.
    """
    query = urllib.parse.urlencode({"redirectUrl": redirect_url})
    body = json.dumps({"redirectUrl": redirect_url, "preUid": "-1"}).encode()
    req = urllib.request.Request(
        f"{WEB_BASE}{VEHICLE_LIST_PATH}?{query}",
        data=body,
        headers=_wire_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise WebSsoError(f"HTTP {exc.code} requesting login URLs: {exc.read()[:200]!r}") from exc

    data = payload.get("data") or {}
    if not data.get("authUrl") or not data.get("callbackUrl"):
        raise WebSsoError(f"no authUrl/callbackUrl in response: {str(payload)[:200]}")
    return LoginUrls(auth_url=data["authUrl"], callback_url=data["callbackUrl"])


def redeem_auth_code(callback_url: str, code: str) -> WebSession:
    """
    Exchange an authorisation code for a session, by GETting the callback.

    `callback_url` must be the one that came back alongside this `code` — its
    `state` is what gets validated. Returns the cookie triple the gateway sets.
    """
    if not code:
        raise WebSsoError("no code supplied")

    parsed = urllib.parse.urlparse(callback_url)
    query = urllib.parse.parse_qs(parsed.query)
    query["code"] = [code]
    url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))

    # Cookies are read off the response directly rather than from a jar, so the
    # caller sees exactly which triple arrived (or that none did).
    req = urllib.request.Request(url, headers=_wire_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        raise WebSsoError(f"callback HTTP {exc.code}: {exc.read()[:200]!r}") from exc

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("code"):
        known = ERROR_MESSAGES.get(payload["code"])
        raise WebSsoError(
            f"redemption rejected: code={payload['code']} "
            f"{known or payload.get('msg') or ''}".strip()
        )

    cookies: dict[str, str] = {}
    for header in headers.get_all("Set-Cookie") or []:
        pair = header.split(";", 1)[0].strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value.strip()

    uid = cookies.get("wasxuid", "") or cookies.get("wasxuid_r", "")
    if not uid:
        raise WebSsoError(f"no session cookie was set; got {sorted(cookies)} or none at all")

    return WebSession(uid=uid, cookies=cookies, device_id=_device_id())


def extract_code(text: str) -> str:
    """
    Pull a `code` out of whatever the user pasted.

    Accepts a full redirect URL, a bare query string, or just the code itself,
    because all three are reasonable things to copy out of an address bar.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text
        found = urllib.parse.parse_qs(query).get("code", [""])[0]
        if found:
            return found
    # A bare token: hex or base64url, no spaces.
    if " " not in text and len(text) >= 16:
        return text
    return ""
