"""Config flow for the XPENG integration."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_GOOGLE,
    AUTH_METHOD_IMPORT,
    CONF_AUTH_METHOD,
    CONF_CERT_DIR,
    CONF_CLIENT_ID,
    CONF_CONFIGURED_DEVICE_ID,
    CONF_ICCID,
    CONF_LOGIN_TOKENS,
    CONF_MQTT_LISTENER_ENABLED,
    CONF_MQTT_LISTENER_TOPICS,
    CONF_MQTT_REFRESH_INTERVAL,
    CONF_MQTT_TOPIC_DISCOVERY,
    CONF_PARKED_SCAN_INTERVAL,
    CONF_PASSWORD,
    CONF_REST_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERVER,
    CONF_TENANT_DATA,
    CONF_TENANT_FILE,
    CONF_TOKEN_FILE,
    CONF_USABLE_BATTERY_KWH,
    CONF_VIN,
    CONF_XPENG_EMAIL,
    CONF_XPENG_PASSWORD,
    DEFAULT_CERT_DIR,
    DEFAULT_MQTT_REFRESH_INTERVAL,
    DEFAULT_PARKED_SCAN_INTERVAL,
    DEFAULT_REST_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERVER,
    DEFAULT_TENANT_FILE,
    DEFAULT_TOKEN_FILE,
    DEFAULT_USABLE_BATTERY_KWH,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

#: `AUTH_METHOD_IMPORT` used to be defined here, with a comment claiming this
#: module was its only reader. It was not: repairs.py imports it from const.py,
#: so the move silently broke that module's import and disabled the Repairs
#: card. It lives in const.py with the other two methods.

#: XPENG's login-stage slider-captcha challenges (declared in xpeng_api as
#: LOGIN_CAPTCHA_CODES). Both mean the same thing to a user: only a human drag
#: in a real browser clears them, which this flow cannot render. Repeated here
#: rather than imported because xpeng_api pulls in cryptography/paho, and this
#: module has to stay importable where the integration's requirements are not
#: installed (the repo's stubbed test harness does exactly that).
#: Where the renewal app writes its pair by default - its own `pair_dir` option.
#: Named here only so the import form can offer the paths as defaults when the
#: pair is already on this host; if the app's `pair_dir` was changed, the two
#: files simply will not be found and the fields stay blank.
_DEFAULT_PAIR_DIR: Final = "/config/xpeng-secrets"

_CAPTCHA_GATE_CODES: Final = frozenset({12040101, 12040106})

#: Substrings that identify the gate when it arrives as an exception message
#: instead of a parsed business code — xpeng_api.login_password() raises a
#: ValueError carrying describe_login_failure()'s text, which names the captcha
#: and the code.
_CAPTCHA_GATE_MARKERS: Final = ("12040101", "12040106", "captcha")


def _entry_options(entry: ConfigEntry) -> dict[str, Any]:
    """An entry's options as a plain dict.

    Home Assistant entries always carry an options mapping; this only tolerates
    the entry objects built by the repo's stubbed test harness, which has none.
    """
    return dict(getattr(entry, "options", None) or {})


class _TokenImportError(ValueError):
    """A pasted token/tenant payload that cannot be imported.

    Carries the config-flow error key so the step can render the exact reason,
    and keeps the payload itself out of the message: these strings reach the HA
    log, and both files are live credentials.
    """

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(detail)
        self.key = key


def _login_captcha_gate(result: Any) -> bool:
    """True when a login step was refused pending XPENG's slider captcha.

    Handles both shapes the gate arrives in: a parsed step result (the business
    code sits under ``body_json``, see xpeng_api._login_signed_step) and the
    exception ``login_password()`` raises when pwdLogin refuses.
    """
    if isinstance(result, BaseException):
        text = str(result).lower()
        return any(marker in text for marker in _CAPTCHA_GATE_MARKERS)
    if isinstance(result, dict):
        parsed = result.get("body_json")
        if isinstance(parsed, dict) and parsed.get("code") in _CAPTCHA_GATE_CODES:
            return True
        return result.get("code") in _CAPTCHA_GATE_CODES
    return False


def _parse_json_object(text: str, *, key: str, what: str) -> dict[str, Any]:
    """Parse pasted JSON into an object, or raise :class:`_TokenImportError`.

    The failure detail deliberately carries no part of the payload.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        raise _TokenImportError(key, f"{what} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _TokenImportError(key, f"{what} is not a JSON object")
    return parsed


def _pair_on_host() -> bool:
    """Whether the renewal app's pair is already sitting on this host.

    True means the import has nothing to ask: the paths are known, the VIN comes out
    of the pair, and no sign-in method needs choosing.
    """
    return os.path.isfile(
        os.path.join(_DEFAULT_PAIR_DIR, "xpeng_tokens.json")
    ) and os.path.isfile(os.path.join(_DEFAULT_PAIR_DIR, "xpeng_tenant.json"))


def _tokens_from_text(text: str) -> dict[str, Any]:
    """The token object inside pasted token-file contents.

    The onboarding tool writes the captured shape ``{"json": {...}}`` (plus a
    redundant ``jsonText`` copy of the same object), while other tools write the
    bare token object. Both are accepted; unwrapping here means the rest of the
    flow only ever sees one shape. Either value may itself be a JSON string.
    """
    raw = _parse_json_object(text, key="invalid_tokens_json", what="The token file")

    inner: Any = raw
    for field in ("json", "jsonText"):
        candidate = raw.get(field)
        if isinstance(candidate, dict):
            inner = candidate
            break
        if isinstance(candidate, str) and candidate.lstrip().startswith("{"):
            inner = _parse_json_object(
                candidate[candidate.find("{"):],
                key="invalid_tokens_json",
                what="The token file",
            )
            break

    if not isinstance(inner, dict) or not inner.get("accessToken"):
        raise _TokenImportError(
            "invalid_tokens", "the token file carries no accessToken"
        )
    return inner


def _tenant_from_text(text: str) -> dict[str, Any]:
    """The tenant object inside pasted tenant-file contents, normalised.

    Validates the two pieces nothing else can work without — ``deviceId`` (the
    token pair is sealed against it) and ``keys`` (the pool that decrypts the
    session) — and returns the in-memory tenant shape, i.e. with ``_key_pool``
    as bytes, which is what `_serialize_tenant()` and the REST layer expect.
    """
    raw = _parse_json_object(text, key="invalid_tenant_json", what="The tenant file")

    if not str(raw.get("deviceId") or "").strip():
        raise _TokenImportError("invalid_tenant", "the tenant carries no deviceId")
    keys = raw.get("keys")
    if not isinstance(keys, dict) or not keys:
        raise _TokenImportError("invalid_tenant", "the tenant carries no keys")

    # deserialize_tenant() runs b64decode() without validation, which silently
    # accepts a hex-mangled or truncated pool (hex characters are also valid
    # base64 characters). Re-encoding proves each value really was base64.
    import base64

    for index, encoded in keys.items():
        try:
            valid = base64.b64encode(base64.b64decode(str(encoded))).decode(
                "ascii"
            ) == str(encoded).strip()
        except Exception:  # noqa: BLE001 - any decode failure is the same answer
            valid = False
        if not valid:
            raise _TokenImportError(
                "invalid_tenant", f"tenant key {index} is not valid base64"
            )

    from . import xpeng_api

    try:
        tenant = xpeng_api.deserialize_tenant(raw)
    except Exception:  # noqa: BLE001 - reported as a form error, not a crash
        raise _TokenImportError(
            "invalid_tenant", "the tenant key pool could not be decoded"
        ) from None
    if not tenant.get("_key_pool"):
        raise _TokenImportError("invalid_tenant", "the tenant key pool is empty")
    return tenant


def _vin_from_tokens(tokens: dict[str, Any]) -> str:
    """A VIN the token file happens to carry, or "".

    The onboarding tool's token file records no VIN (only the session tokens),
    so most imports have to ask for it — but a file written by another capture
    path may carry one, and then nothing has to be typed.
    """
    candidates: list[Any] = [tokens.get("vin"), tokens.get("VIN")]
    for field in ("vehicle", "vehicleInfo", "data"):
        nested = tokens.get(field)
        if isinstance(nested, dict):
            candidates.extend((nested.get("vin"), nested.get("VIN")))
    for candidate in candidates:
        value = str(candidate or "").strip().upper()
        if len(value) == 17 and value.isascii() and value.isalnum():
            return value
    return ""


def _import_mqtt_credentials(
    tokens: dict[str, Any], tenant: dict[str, Any]
) -> tuple[str, str, str]:
    """The MQTT client id, password and server for an imported pair.

    Prefers the pair the onboarding tool minted (``tokens["mqttAccount"]``) and
    otherwise builds the documented shape from the account uid and the tenant's
    device id. An empty password is deliberate and already supported: the
    integration mints one through the API on its first refresh, which is why
    tools/configure_ha_entry.py stores an empty password too — so importing
    needs no network call of its own.
    """
    account = tokens.get("mqttAccount")
    account = account if isinstance(account, dict) else {}

    device_id = str(tenant.get("deviceId") or "").strip()
    client_id = str(account.get("clientId") or "").strip()
    if not client_id:
        uid = str(tokens.get("uid") or "").strip()
        if not uid:
            raise _TokenImportError(
                "invalid_tokens",
                "the token file has neither an mqttAccount clientId nor a uid",
            )
        client_id = f"xmart:app:{uid}:{device_id}"
    if len(client_id.split(":")) < 3:
        raise _TokenImportError("invalid_tokens", "the MQTT client id is malformed")

    return (
        client_id,
        str(account.get("password") or ""),
        str(account.get("serverSsl") or DEFAULT_SERVER),
    )


def _read_text_file(path: str, key: str) -> str:
    """Read a file the user pointed the import step at.

    Runs in an executor (it is blocking I/O) and reports a failure as the flow
    error for that field rather than a traceback.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        raise _TokenImportError(key, "the file could not be read") from None


class XPengConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for XPENG."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str = ""
        self._password: str = ""
        self._vin: str = ""
        self._tenant: dict[str, Any] | None = None
        self._xid: str = ""
        self._auth_method: str = AUTH_METHOD_GOOGLE
        self._google_url: str = ""
        self._google_verifier: str = ""
        self._reauth_mode: bool = False
        # This install's device id, minted lazily on first API call. The flow
        # runs before any config entry exists, so it cannot rely on the
        # process-wide override that __init__.py publishes at entry setup — and
        # the tenant key pool is sealed against whatever id fetches it, so the
        # id used for the tenant MUST be the id the entry ends up storing.
        self._device_id: str = ""

    def _ensure_device_id(self) -> str:
        """This flow's device id, minted once and reused by every API call."""
        if not self._device_id:
            from . import xpeng_api

            self._device_id = xpeng_api.mint_device_id()
        return self._device_id

    def _api_config(self) -> dict[str, str]:
        """Config dict for the REST layer, carrying this flow's device id.

        The shipped default no longer pins a device id, so passing `None` here
        would make every call raise "No device id configured".
        """
        return {"ConfiguredDeviceId": self._ensure_device_id()}

    def _adopt_entry_device_id(self, entry: ConfigEntry) -> str:
        """Reuse the id already stored on an entry, else keep/mint one.

        Checked in options first (where __init__.py persists it) and then in
        data, so a reauth or reconfigure never rotates an existing install's
        identity — and never drops one it found.
        """
        stored = str(
            _entry_options(entry).get(CONF_CONFIGURED_DEVICE_ID)
            or entry.data.get(CONF_CONFIGURED_DEVICE_ID)
            or ""
        ).strip()
        if stored:
            self._device_id = stored
        return self._ensure_device_id()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: pick an auth method and give the VIN.

        Nothing is asked when a pair is already on this host. The renewal app puts one
        in pair_dir, and there is then nothing to decide - importing it is the only
        thing this flow can usefully do - so it goes straight to that form with the
        paths filled in. Asking anyway meant a required VIN box and a sign-in method
        to choose on a screen whose answer was already on disk.

        The questions are for the other case, someone holding a pair to paste. XPENG
        gates most accounts' sign-in behind its own slider captcha, which only a human
        dragging it in a real browser can clear - so importing a pair minted outside
        HA is the path that reliably completes. Google and email are kept for accounts
        XPENG does not gate.
        """
        errors: dict[str, str] = {}

        if user_input is None and _pair_on_host():
            return await self.async_step_import_tokens()

        if user_input is not None:
            vin = (user_input.get(CONF_VIN) or "").strip().upper()
            method = user_input.get(CONF_AUTH_METHOD, AUTH_METHOD_IMPORT)

            # Only the sign-in routes need the VIN here. The import step reads it out
            # of the pair, so asking for it on the way in duplicated a question that
            # has an answer - and made it look required when it is not.
            if not vin and method != AUTH_METHOD_IMPORT:
                errors[CONF_VIN] = "required"
            else:
                self._vin = vin
                self._auth_method = method
                if method == AUTH_METHOD_EMAIL:
                    return await self.async_step_email_password()
                if method == AUTH_METHOD_IMPORT:
                    return await self.async_step_import_tokens()
                return await self.async_step_google()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_VIN, default=""): str,
                    vol.Required(
                        CONF_AUTH_METHOD, default=AUTH_METHOD_IMPORT
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": AUTH_METHOD_IMPORT, "label": "Import tokens from the onboarding tool (recommended)"},
                                {"value": AUTH_METHOD_GOOGLE, "label": "Sign in with Google (usually blocked by the slider captcha)"},
                                {"value": AUTH_METHOD_EMAIL, "label": "Email + password (usually blocked by the slider captcha)"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_email_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Email + password path (classic flow)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input.get(CONF_XPENG_EMAIL, "").strip()
            password = user_input.get(CONF_XPENG_PASSWORD, "").strip()

            if not email:
                errors[CONF_XPENG_EMAIL] = "required"
            elif not password:
                errors[CONF_XPENG_PASSWORD] = "required"
            else:
                self._email = email
                self._password = password

                # Fetch tenant and send verification code
                try:
                    from . import xpeng_api

                    api_config = self._api_config()
                    tenant = await self.hass.async_add_executor_job(
                        xpeng_api.fetch_tenant, api_config
                    )
                    self._tenant = tenant

                    result = await self.hass.async_add_executor_job(
                        xpeng_api.login_get_state, api_config, tenant, email
                    )
                    if _login_captcha_gate(result):
                        # 12040101/12040106: XPENG wants its slider solved. That
                        # needs a human in a real browser, which this flow has no
                        # way to provide, so stop here rather than email a code
                        # that cannot lead anywhere and say what to do instead.
                        _LOGGER.warning(
                            "Email sign-in is captcha-gated; the token-import "
                            "path is required for this account"
                        )
                        errors["base"] = "captcha_gated"
                    else:
                        self._xid = result.get("xid") or ""

                        await self.hass.async_add_executor_job(
                            lambda: xpeng_api.login_send_code(
                                api_config, tenant, email, xid=self._xid
                            )
                        )

                        return await self.async_step_email_code()
                except Exception as err:
                    if _login_captcha_gate(err):
                        _LOGGER.warning("Email sign-in is captcha-gated: %s", err)
                        errors["base"] = "captcha_gated"
                    else:
                        _LOGGER.error("Login init failed: %s", err)
                        errors["base"] = "login_failed"

        return self.async_show_form(
            step_id="email_password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_XPENG_EMAIL): str,
                    vol.Required(CONF_XPENG_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import the token + tenant pair the external onboarding tool wrote.

        The slider captcha on XPENG's sign-in cannot be solved from inside Home
        Assistant (see `_login_captcha_gate`), so this is the only path that can
        complete for a gated account: the user runs
        `tools/onboard_email_login.py` on a machine with a real browser and
        drags the slider once, then pastes the two files it wrote — or gives
        their paths when they are on the HA host. Both are stored in the entry
        data, which keeps `_load_api_context()` on a single credential source.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            vin = (user_input.get(CONF_VIN) or "").strip().upper() or self._vin

            try:
                tokens_text = (user_input.get("tokens_json") or "").strip()
                tenant_text = (user_input.get("tenant_json") or "").strip()

                if not tokens_text:
                    tokens_path = (user_input.get("tokens_path") or "").strip()
                    if not tokens_path:
                        raise _TokenImportError(
                            "invalid_tokens",
                            "no token file contents or path was given",
                        )
                    tokens_text = await self.hass.async_add_executor_job(
                        _read_text_file, tokens_path, "invalid_tokens_path"
                    )
                if not tenant_text:
                    tenant_path = (user_input.get("tenant_path") or "").strip()
                    if not tenant_path:
                        raise _TokenImportError(
                            "invalid_tenant",
                            "no tenant file contents or path was given",
                        )
                    tenant_text = await self.hass.async_add_executor_job(
                        _read_text_file, tenant_path, "invalid_tenant_path"
                    )

                tokens = _tokens_from_text(tokens_text)
                tenant = _tenant_from_text(tenant_text)

                if not vin:
                    vin = _vin_from_tokens(tokens)
                if not vin:
                    raise _TokenImportError(
                        "invalid_vin",
                        "the tokens carry no VIN and none was entered",
                    )

                mqtt = _import_mqtt_credentials(tokens, tenant)
            except _TokenImportError as err:
                # Log the reason but never the payload: both files are live
                # credentials and this log is what a support request attaches.
                _LOGGER.warning("Token import rejected: %s", err)
                errors["base"] = err.key
            except Exception:
                _LOGGER.exception("Token import failed")
                errors["base"] = "unknown"
            else:
                # Record which account this pair belongs to: the tokens are
                # a tenant-sealed blob with no account claim in them, so
                # this is the only place the email exists. The tool puts
                # it in the token file; a caller may also type it.
                account_email = str(
                    tokens.get("account_email")
                    or (user_input.get(CONF_XPENG_EMAIL) or "").strip()
                ).strip()
                return await self._finish_import(
                    tokens, tenant, vin, mqtt, account_email
                )

        # The renewal app writes the pair to a fixed directory. When it is already
        # sitting there, offer those paths as the defaults, so the documented
        # bootstrap is "accept the form" rather than "type two container paths
        # correctly". Filled in only when BOTH files are present, and only on the
        # first show, so an install whose pair is elsewhere - or on a desktop, to
        # be pasted - still gets blank fields.
        default_tokens_path = ""
        default_tenant_path = ""
        if user_input is None and _pair_on_host():
            default_tokens_path = os.path.join(_DEFAULT_PAIR_DIR, "xpeng_tokens.json")
            default_tenant_path = os.path.join(_DEFAULT_PAIR_DIR, "xpeng_tenant.json")

        return self.async_show_form(
            step_id="import_tokens",
            data_schema=vol.Schema(
                {
                    vol.Optional("tokens_json", default=""): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional("tokens_path", default=default_tokens_path): str,
                    vol.Optional("tenant_json", default=""): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional("tenant_path", default=default_tenant_path): str,
                    vol.Optional(CONF_VIN, default=self._vin): str,
                    # Which account the pair belongs to. Normally the onboarding
                    # tool already recorded this in the token file and this can
                    # be left blank - it exists so an entry imported before that
                    # can be given the value, and because the email is the one
                    # account detail the tokens themselves cannot provide.
                    vol.Optional(
                        CONF_XPENG_EMAIL,
                        default=str(self._email or ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def _finish_import(
        self,
        tokens: dict[str, Any],
        tenant: dict[str, Any],
        vin: str,
        mqtt: tuple[str, str, str],
        account_email: str = "",
    ) -> ConfigFlowResult:
        """Create (or, in reauth, update) the entry from an imported pair."""
        client_id, password, server = mqtt

        # The tenant's own device id wins, always: its key pool is sealed with
        # md5(deviceId) and the token envelope was minted for it, so no other id
        # can use this pair. Seeding self._device_id means _ensure_device_id()
        # below persists this id instead of minting a fresh one.
        self._device_id = str(tenant.get("deviceId") or "").strip()

        data = {
            CONF_CLIENT_ID: client_id,
            CONF_PASSWORD: password,
            CONF_SERVER: server,
            CONF_VIN: vin,
            # Record how the entry authenticates, so reauth routes it back here
            # where a freshly minted pair can be pasted.
            CONF_AUTH_METHOD: AUTH_METHOD_IMPORT,
            CONF_LOGIN_TOKENS: tokens,
            CONF_TENANT_DATA: _serialize_tenant(tenant),
        }
        # Stored on the entry, not only in the token file: entry data lives in
        # HA's own storage, so this survives reboots and survives the file being
        # deleted or replaced. Only written when known, so a re-import whose
        # source lacks it cannot blank a value already recorded.
        if account_email:
            data[CONF_XPENG_EMAIL] = account_email
        options = {
            CONF_CONFIGURED_DEVICE_ID: self._ensure_device_id(),
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_MQTT_REFRESH_INTERVAL: DEFAULT_MQTT_REFRESH_INTERVAL,
            CONF_REST_SCAN_INTERVAL: DEFAULT_REST_SCAN_INTERVAL,
        }

        if self._reauth_mode:
            entry = self.hass.config_entries.async_get_entry(
                self.context.get("entry_id", "")
            )
            if not entry:
                return self.async_abort(reason="unknown")
            self.hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, **data},
                options={**_entry_options(entry), **options},
            )
            # A fresh pair means the "credentials need re-onboarding" Repairs
            # card is stale; clear it rather than leave the user staring at a
            # problem they just fixed.
            try:
                from . import _clear_reauth_repair

                _clear_reauth_repair(self.hass, entry)
            except Exception:  # pragma: no cover - repair card is optional
                pass
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(vin)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"XPENG {vin[-6:]}",
            data=data,
            options=options,
        )

    async def async_step_email_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Collect email verification code, validate, and login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("email_code", "").strip()
            if not code:
                errors["email_code"] = "required"
            else:
                try:
                    from . import xpeng_api

                    api_config = self._api_config()
                    tenant = self._tenant
                    email = self._email
                    password = self._password

                    # Validate code
                    result = await self.hass.async_add_executor_job(
                        lambda: xpeng_api.login_validate_code(
                            api_config, tenant, email, code, xid=self._xid
                        )
                    )
                    xid = result.get("xid") or self._xid

                    # Password login
                    tokens = await self.hass.async_add_executor_job(
                        lambda: xpeng_api.login_password(
                            api_config, tenant, email, password, xid=xid
                        )
                    )

                    # Get initial MQTT credentials
                    mqtt_creds = await self.hass.async_add_executor_job(
                        xpeng_api.create_mqtt_account, api_config, tenant, tokens
                    )

                    client_id = mqtt_creds["clientId"]
                    mqtt_password = mqtt_creds["password"]
                    server = mqtt_creds.get(
                        "serverSsl", DEFAULT_SERVER
                    )

                    # Unique by VIN
                    await self.async_set_unique_id(self._vin)
                    self._abort_if_unique_id_configured()

                    # Prepare tenant data for storage (serialize key pool)
                    tenant_store = _serialize_tenant(tenant)

                    return self.async_create_entry(
                        title=f"XPENG {self._vin[-6:]}",
                        data={
                            CONF_CLIENT_ID: client_id,
                            CONF_PASSWORD: mqtt_password,
                            CONF_SERVER: server,
                            CONF_VIN: self._vin,
                            CONF_XPENG_EMAIL: email,
                            CONF_XPENG_PASSWORD: password,
                            CONF_AUTH_METHOD: AUTH_METHOD_EMAIL,
                            CONF_LOGIN_TOKENS: tokens,
                            CONF_TENANT_DATA: tenant_store,
                        },
                        options={
                            CONF_CONFIGURED_DEVICE_ID: self._ensure_device_id(),
                            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                            CONF_MQTT_REFRESH_INTERVAL: DEFAULT_MQTT_REFRESH_INTERVAL,
                            CONF_REST_SCAN_INTERVAL: DEFAULT_REST_SCAN_INTERVAL,
                        },
                    )

                except Exception as err:
                    if _login_captcha_gate(err):
                        # pwdLogin refused with 12040106 even after the code: the
                        # account is slider-gated, so no amount of code entry can
                        # finish this. Point at the path that can.
                        _LOGGER.warning("Email sign-in is captcha-gated: %s", err)
                        errors["base"] = "captcha_gated"
                    else:
                        _LOGGER.error("Login failed: %s", err)
                        errors["base"] = "login_failed"

        return self.async_show_form(
            step_id="email_code",
            data_schema=vol.Schema(
                {
                    vol.Required("email_code"): str,
                }
            ),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def async_step_google(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with Google — build the OAuth link and collect the redirect.

        The user opens the link, signs in with Google, and pastes back the
        redirect URL (the browser cannot open the custom scheme; the auth code
        is in its address bar).

        This path is NOT captcha-free. XPENG applies the same slider gate to the
        account after the Google id token is exchanged, and this flow has no way
        to render or solve it — a gated account has to use the token-import path.
        """
        from . import google_login as gl

        errors: dict[str, str] = {}

        if user_input is not None:
            code = gl.extract_code(user_input.get("redirect_url", ""))
            # A sign-in driven through the system web-auth session
            # (tools/oauth_capture.swift) generates its own PKCE pair, because
            # a plain browser never receives the custom-scheme redirect at all
            # — Google bounces it to the homepage. That helper must therefore
            # hand back the matching verifier: the code cannot be exchanged
            # without it, and self._google_verifier belongs to a different pair.
            verifier = (
                user_input.get("code_verifier") or ""
            ).strip() or self._google_verifier
            if not code:
                errors["redirect_url"] = "invalid_code"
            else:
                try:
                    from . import xpeng_api

                    # Reauth/reconfigure enters this step with an entry already
                    # on screen: keep that install's id (or, on an entry that
                    # predates this field, persist the one we are about to use).
                    entry = self.hass.config_entries.async_get_entry(
                        self.context.get("entry_id", "")
                    )
                    if entry is not None:
                        self._adopt_entry_device_id(entry)

                    api_config = self._api_config()
                    id_token = await self.hass.async_add_executor_job(
                        gl.exchange_code, code, verifier
                    )
                    tenant = await self.hass.async_add_executor_job(
                        xpeng_api.fetch_tenant, api_config
                    )
                    self._tenant = tenant
                    tokens = await self.hass.async_add_executor_job(
                        gl.google_auth_login, xpeng_api, api_config, tenant, id_token
                    )
                    return await self._finish_google(xpeng_api, tenant, tokens, id_token)
                except gl.GoogleLoginError as err:
                    if _login_captcha_gate(err):
                        _LOGGER.warning("Google sign-in is captcha-gated: %s", err)
                        errors["base"] = "captcha_gated"
                    else:
                        _LOGGER.warning("Google sign-in failed: %s", err)
                        errors["base"] = "login_failed"
                except Exception as err:
                    if _login_captcha_gate(err):
                        _LOGGER.warning("Google sign-in is captcha-gated: %s", err)
                        errors["base"] = "captcha_gated"
                    else:
                        _LOGGER.exception("Google sign-in failed")
                        errors["base"] = "unknown"

        # Build the OAuth URL once per flow — the verifier must match the link
        # the user actually opened, so it is stored on the instance.
        if not self._google_url:
            url, verifier = gl.build_auth_url()
            self._google_url = url
            self._google_verifier = verifier
            # Also log it. The URL is not a secret — the PKCE verifier stays
            # server-side, so a code obtained from this URL cannot be exchanged
            # without it — and having it in the log means an admin can still
            # complete the sign-in when the form's description fails to render
            # (which happens if this step is missing from strings.json /
            # translations/en.json; seen 2026-10-04).
            _LOGGER.info(
                "Google sign-in URL for entry %s: %s",
                self.context.get("entry_id", "?"), url,
            )

        return self.async_show_form(
            step_id="google",
            data_schema=vol.Schema(
                {
                    vol.Required("redirect_url"): str,
                    vol.Optional("code_verifier", default=""): str,
                }
            ),
            description_placeholders={"url": self._google_url},
            errors=errors,
        )

    async def _finish_google(self, xpeng_api, tenant, tokens, id_token) -> ConfigFlowResult:
        """Mint MQTT credentials and create (or, in reauth, update) the entry."""
        from . import google_login as gl

        mqtt_creds = await self.hass.async_add_executor_job(
            xpeng_api.create_mqtt_account, self._api_config(), tenant, tokens
        )
        email = gl.decode_id_token_claims(id_token).get("email", "")

        if self._reauth_mode:
            entry = self.hass.config_entries.async_get_entry(
                self.context.get("entry_id", "")
            )
            if not entry:
                return self.async_abort(reason="unknown")
            # Never overwrite an id this flow already signed with: the MQTT
            # credentials above were minted under it and the tenant is sealed
            # against it, so the entry must store the same one.
            device_id = self._device_id or self._adopt_entry_device_id(entry)
            tenant_store = _serialize_tenant(tenant)
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_CLIENT_ID: mqtt_creds["clientId"],
                    CONF_PASSWORD: mqtt_creds["password"],
                    CONF_SERVER: mqtt_creds.get("serverSsl", DEFAULT_SERVER),
                    # Record how this entry now authenticates, so the next
                    # expiry goes straight back to the Google path instead of
                    # having to re-derive it from missing credentials.
                    CONF_AUTH_METHOD: AUTH_METHOD_GOOGLE,
                    CONF_LOGIN_TOKENS: tokens,
                    CONF_TENANT_DATA: tenant_store,
                    **({CONF_XPENG_EMAIL: email} if email else {}),
                },
                # Keep the identity the reauth signed with; entries created
                # before this option existed get it persisted here.
                options={
                    **_entry_options(entry),
                    CONF_CONFIGURED_DEVICE_ID: device_id,
                },
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(self._vin)
        self._abort_if_unique_id_configured()

        tenant_store = _serialize_tenant(tenant)
        return self.async_create_entry(
            title=f"XPENG {self._vin[-6:]}",
            data={
                CONF_CLIENT_ID: mqtt_creds["clientId"],
                CONF_PASSWORD: mqtt_creds["password"],
                CONF_SERVER: mqtt_creds.get("serverSsl", DEFAULT_SERVER),
                CONF_VIN: self._vin,
                CONF_XPENG_EMAIL: email,
                CONF_XPENG_PASSWORD: "",  # no password on the Google path
                CONF_AUTH_METHOD: AUTH_METHOD_GOOGLE,
                CONF_LOGIN_TOKENS: tokens,
                CONF_TENANT_DATA: tenant_store,
            },
            options={
                CONF_CONFIGURED_DEVICE_ID: self._ensure_device_id(),
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_MQTT_REFRESH_INTERVAL: DEFAULT_MQTT_REFRESH_INTERVAL,
                CONF_REST_SCAN_INTERVAL: DEFAULT_REST_SCAN_INTERVAL,
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth when login tokens expire."""
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if not entry:
            return self.async_abort(reason="unknown")

        self._email = entry.data.get(CONF_XPENG_EMAIL, "")
        self._password = entry.data.get(CONF_XPENG_PASSWORD, "")
        self._vin = entry.data.get(CONF_VIN, "")
        # Reauth must sign as the device this entry already is; only an entry
        # with no stored id (created before the option existed) mints a new one.
        self._adopt_entry_device_id(entry)

        # The auth method recorded at setup decides which reauth applies. A
        # Google-created entry has no password to re-authenticate with, so it
        # goes back through the Google sign-in flow instead.
        #
        # Entries imported from a token / MQTT-credential file predate this
        # field and carry no account email or password at all, so the email
        # path can never succeed for them — route those to Google too, which
        # needs nothing stored. Without this they hit the "no_credentials"
        # abort below and reauth could never complete (seen 2026-10-04).
        #
        # An entry created by the token-import step records AUTH_METHOD_IMPORT:
        # its pair was minted outside HA by the onboarding tool, and re-running
        # that tool is the only way to renew it (there is no stored password,
        # and the Google path cannot reproduce the imported device identity),
        # so it goes straight back to the import step.
        auth_method = entry.data.get(CONF_AUTH_METHOD)
        if auth_method is None:
            auth_method = (
                AUTH_METHOD_EMAIL
                if self._email and self._password
                else AUTH_METHOD_GOOGLE
            )
        if auth_method == AUTH_METHOD_IMPORT:
            self._reauth_mode = True
            return await self.async_step_import_tokens()
        if auth_method == AUTH_METHOD_GOOGLE:
            self._reauth_mode = True
            self._google_url = ""
            self._google_verifier = ""
            return await self.async_step_google()

        if not self._email or not self._password:
            # No stored credentials — can't auto-reauth
            return self.async_abort(reason="no_credentials")

        # Send verification code automatically
        try:
            from . import xpeng_api

            api_config = self._api_config()
            tenant = await self.hass.async_add_executor_job(
                xpeng_api.fetch_tenant, api_config
            )
            self._tenant = tenant

            result = await self.hass.async_add_executor_job(
                xpeng_api.login_get_state, api_config, tenant, self._email
            )
            if _login_captcha_gate(result):
                # The account cannot be re-authenticated from here at all: its
                # sign-in needs a slider drag in a real browser, which is the
                # onboarding tool's job. Abort with the path that works instead
                # of emailing a code that can never be used.
                _LOGGER.warning(
                    "Reauth for %s is captcha-gated; the onboarding tool and the "
                    "token-import path are required",
                    self._email,
                )
                return self.async_abort(reason="captcha_gated")

            self._xid = result.get("xid") or ""

            await self.hass.async_add_executor_job(
                lambda: xpeng_api.login_send_code(
                    api_config, tenant, self._email, xid=self._xid
                )
            )
            _LOGGER.info("Reauth: verification code sent to %s", self._email)
        except Exception as err:
            if _login_captcha_gate(err):
                _LOGGER.warning("Reauth is captcha-gated: %s", err)
                return self.async_abort(reason="captcha_gated")
            _LOGGER.error("Reauth: failed to send code: %s", err)
            return self.async_abort(reason="login_failed")

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth code entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("email_code", "").strip()
            if not code:
                errors["email_code"] = "required"
            else:
                try:
                    from . import xpeng_api

                    api_config = self._api_config()
                    tenant = self._tenant
                    email = self._email
                    password = self._password

                    result = await self.hass.async_add_executor_job(
                        lambda: xpeng_api.login_validate_code(
                            api_config, tenant, email, code, xid=self._xid
                        )
                    )
                    xid = result.get("xid") or self._xid

                    tokens = await self.hass.async_add_executor_job(
                        lambda: xpeng_api.login_password(
                            api_config, tenant, email, password, xid=xid
                        )
                    )

                    mqtt_creds = await self.hass.async_add_executor_job(
                        xpeng_api.create_mqtt_account, api_config, tenant, tokens
                    )

                    tenant_store = _serialize_tenant(tenant)

                    entry = self.hass.config_entries.async_get_entry(
                        self.context.get("entry_id", "")
                    )
                    if entry:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={
                                **entry.data,
                                CONF_CLIENT_ID: mqtt_creds["clientId"],
                                CONF_PASSWORD: mqtt_creds["password"],
                                CONF_LOGIN_TOKENS: tokens,
                                CONF_TENANT_DATA: tenant_store,
                            },
                            # Persist the identity this reauth signed with so a
                            # pre-existing entry keeps it from now on.
                            options={
                                **_entry_options(entry),
                                CONF_CONFIGURED_DEVICE_ID: self._ensure_device_id(),
                            },
                        )
                        # Also update token file if configured
                        token_file = entry.options.get(CONF_TOKEN_FILE, "").strip()
                        if token_file:
                            try:
                                import json
                                tokens["mqttAccount"] = mqtt_creds
                                with open(token_file, "w") as f:
                                    json.dump({"json": tokens}, f, indent=2)
                                _LOGGER.info("Reauth: updated token file %s", token_file)
                            except Exception as err:
                                _LOGGER.warning("Reauth: failed to update token file: %s", err)

                        await self.hass.config_entries.async_reload(entry.entry_id)

                    return self.async_abort(reason="reauth_successful")

                except Exception as err:
                    if _login_captcha_gate(err):
                        # pwdLogin refused with 12040106 even with a valid code:
                        # the account is slider-gated, so this reauth can never
                        # complete from inside Home Assistant.
                        _LOGGER.warning("Reauth is captcha-gated: %s", err)
                        return self.async_abort(reason="captcha_gated")
                    _LOGGER.error("Reauth failed: %s", err)
                    errors["base"] = "login_failed"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("email_code"): str,
                }
            ),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration (update client_id + password manually)."""
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if not entry:
            return self.async_abort(reason="unknown")

        # Reconciling credentials by hand must not change who this install is.
        self._adopt_entry_device_id(entry)

        errors: dict[str, str] = {}
        current = entry.data

        if user_input is not None:
            client_id = user_input.get(CONF_CLIENT_ID, "").strip()
            password = user_input.get(CONF_PASSWORD, "").strip()

            if not client_id:
                errors[CONF_CLIENT_ID] = "required"
            elif not password:
                errors[CONF_PASSWORD] = "required"
            elif len(client_id.split(":")) < 3:
                errors[CONF_CLIENT_ID] = "invalid_client_id"
            else:
                server = user_input.get(
                    CONF_SERVER, current.get(CONF_SERVER, DEFAULT_SERVER)
                ).strip()

                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **current,
                        CONF_CLIENT_ID: client_id,
                        CONF_PASSWORD: password,
                        CONF_SERVER: server,
                    },
                    options={
                        **_entry_options(entry),
                        CONF_CONFIGURED_DEVICE_ID: self._ensure_device_id(),
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigured")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_ID,
                        default=current.get(CONF_CLIENT_ID, ""),
                    ): str,
                    vol.Required(
                        CONF_PASSWORD,
                        default=current.get(CONF_PASSWORD, ""),
                    ): str,
                    vol.Optional(
                        CONF_SERVER,
                        default=current.get(CONF_SERVER, DEFAULT_SERVER),
                    ): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> XPengOptionsFlow:
        """Get the options flow for this handler."""
        return XPengOptionsFlow()


class XPengOptionsFlow(OptionsFlow):
    """Handle XPENG options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # An explicit cert dir must actually hold the mTLS material,
            # otherwise the integration can never connect to the broker.
            # Blank means "use the default dir", which is a valid choice.
            # mqtt_client is imported here rather than at module level because
            # it pulls in paho-mqtt: keeping this module's import graph light
            # means the config/options UI still loads where the integration's
            # requirements are not installed (the repo's stubbed test harness
            # does exactly that). The cert file list itself lives in exactly
            # one place — mqtt_client.CERT_FILES.
            from .mqtt_client import missing_cert_files

            cert_dir = str(user_input.get(CONF_CERT_DIR, "")).strip()
            missing = missing_cert_files(cert_dir) if cert_dir else []
            if missing:
                _LOGGER.error(
                    "Certificate directory %s is missing: %s",
                    cert_dir,
                    ", ".join(str(path) for path in missing),
                )
                errors[CONF_CERT_DIR] = "cert_files_missing"
            else:
                return self.async_create_entry(title="", data=user_input)

        # On a failed submit, re-render what the user typed so the cert dir can
        # be corrected without retyping the rest of the form.
        current = {**self.config_entry.options, **(user_input or {})}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=10, max=3600)),
                    vol.Optional(
                        CONF_PARKED_SCAN_INTERVAL,
                        default=current.get(
                            CONF_PARKED_SCAN_INTERVAL,
                            DEFAULT_PARKED_SCAN_INTERVAL,
                        ),
                    ): vol.All(int, vol.Range(min=60, max=7200)),
                    vol.Optional(
                        CONF_MQTT_REFRESH_INTERVAL,
                        default=current.get(
                            CONF_MQTT_REFRESH_INTERVAL,
                            DEFAULT_MQTT_REFRESH_INTERVAL,
                        ),
                    ): vol.All(int, vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_REST_SCAN_INTERVAL,
                        default=current.get(
                            CONF_REST_SCAN_INTERVAL,
                            DEFAULT_REST_SCAN_INTERVAL,
                        ),
                    ): vol.All(int, vol.Range(min=60, max=3600)),
                    vol.Optional(
                        CONF_MQTT_LISTENER_ENABLED,
                        default=current.get(
                            CONF_MQTT_LISTENER_ENABLED, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_MQTT_LISTENER_TOPICS,
                        default=current.get(
                            CONF_MQTT_LISTENER_TOPICS, ""
                        ),
                    ): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(
                        CONF_MQTT_TOPIC_DISCOVERY,
                        default=current.get(
                            CONF_MQTT_TOPIC_DISCOVERY, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_TOKEN_FILE,
                        default=current.get(CONF_TOKEN_FILE, ""),
                    ): str,
                    vol.Optional(
                        CONF_TENANT_FILE,
                        default=current.get(
                            CONF_TENANT_FILE, DEFAULT_TENANT_FILE
                        ),
                    ): str,
                    vol.Optional(
                        CONF_CERT_DIR,
                        default=current.get(
                            CONF_CERT_DIR, DEFAULT_CERT_DIR
                        ),
                    ): str,
                    vol.Optional(
                        CONF_ICCID,
                        default=current.get(CONF_ICCID, ""),
                    ): str,
                    vol.Optional(
                        CONF_USABLE_BATTERY_KWH,
                        default=current.get(
                            CONF_USABLE_BATTERY_KWH,
                            DEFAULT_USABLE_BATTERY_KWH,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=10, max=150)),
                }
            ),
            errors=errors,
        )


def _serialize_tenant(tenant: dict[str, Any]) -> dict[str, Any]:
    """Serialize tenant data for storage in config entry.

    Converts binary key pool values to hex strings for JSON serialization.
    """
    import base64

    result = {
        "teeId": tenant.get("teeId", ""),
        "version": tenant.get("version", 2),
        # The device id belongs to the binding, not just to the metadata: the
        # key pool is sealed with md5(deviceId) as its AES key, so a tenant
        # stored without it can no longer be matched to the identity that
        # fetched it. __init__.py adopts it from here (and from the tenant
        # file) when an entry's configured_device_id is empty.
        "deviceId": tenant.get("deviceId", ""),
    }
    key_pool = tenant.get("_key_pool", {})
    keys_hex: dict[str, str] = {}
    for idx, key_bytes in key_pool.items():
        if isinstance(key_bytes, bytes):
            keys_hex[str(idx)] = base64.b64encode(key_bytes).decode("ascii")
        else:
            keys_hex[str(idx)] = str(key_bytes)
    result["keys"] = keys_hex
    return result
