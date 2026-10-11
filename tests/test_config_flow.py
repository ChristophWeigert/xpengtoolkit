#!/usr/bin/env python3
"""Exercise the Google sign-in config flow without Home Assistant or network.

Config flows are the highest-risk part of an integration to get wrong — a bug
crashes the setup UI, and you only find out when a user reports it. This test
stubs Home Assistant and the network calls so the Google path can be walked end
to end and the entry data asserted.

Run from the repo root:

    python3 tests/test_config_flow.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import pathlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

PKG_DIR = _component.component_dir()


# ── Home Assistant stubs ──────────────────────────────────────────────────

class _ConfigFlowResult(dict):
    pass


class ConfigFlow:
    """Minimal stand-in. Records instead of driving HA."""

    context: dict = {}  # HA sets this from outside; the flow's own __init__ does not.

    def __init_subclass__(cls, **kwargs):
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)

    def __init__(self):
        self.hass = None  # set by the test
        self.context = {}
        self.flow_id = "test-flow"

    def async_show_form(self, *, step_id, data_schema=None, description_placeholders=None, errors=None):
        return _ConfigFlowResult(
            type="form",
            step_id=step_id,
            description_placeholders=description_placeholders,
            errors=errors or {},
        )

    def async_create_entry(self, *, title, data, options=None):
        return _ConfigFlowResult(type="create_entry", title=title, data=data, options=options)

    async def async_set_unique_id(self, unique_id):
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        pass

    def async_abort(self, *, reason):
        return _ConfigFlowResult(type="abort", reason=reason)


class ConfigEntry:
    def __init__(self, data=None, entry_id="entry-1"):
        self.data = data or {}
        self.entry_id = entry_id


class OptionsFlow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


def callback(fn):
    return fn


def _sel(*args, **kwargs):
    return object()


def _install_ha_stubs():
    ha = types.ModuleType("homeassistant")

    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigFlow = ConfigFlow
    ce.ConfigEntry = ConfigEntry
    ce.ConfigFlowResult = _ConfigFlowResult
    ce.OptionsFlow = OptionsFlow

    core = types.ModuleType("homeassistant.core")
    core.callback = callback

    helpers = types.ModuleType("homeassistant.helpers")
    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.TextSelector = _sel
    selector.TextSelectorConfig = _sel
    selector.SelectSelector = _sel
    selector.SelectSelectorConfig = _sel
    selector.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown")

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.config_entries"] = ce
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.selector"] = selector
    helpers.selector = selector
    ha.config_entries = ce
    ha.core = core
    ha.helpers = helpers


def _install_voluptuous_stub():
    vol = types.ModuleType("voluptuous")

    class _Marker:
        def __init__(self, key, default=None):
            self.key = key
            self.default = default

    def Schema(x, *a, **k):
        return x

    vol.Schema = Schema
    vol.Required = lambda key, default=None: _Marker(key, default)
    vol.Optional = lambda key, default=None: _Marker(key, default)
    vol.All = lambda *a, **k: a[0] if a else None
    vol.Range = lambda **k: None
    vol.Coerce = lambda t: t
    sys.modules["voluptuous"] = vol


class _AutoMeta(type):
    """Class-attribute fallback: SensorDeviceClass.BATTERY -> 'battery'."""

    def __getattr__(cls, name):
        return name.lower()


def _install_const_stub():
    """Stub custom_components.xpeng.const.

    The real const.py subclasses HA's entity-description dataclasses with
    `kw_only=True`, which needs Python 3.10+ — the test runner here is 3.9. The
    config flow only needs the constant *values*, so this stub returns the real
    string for the keys the test asserts on and a sensible value for the rest.
    """
    real = {
        "CONF_CLIENT_ID": "client_id",
        "CONF_PASSWORD": "password",
        "CONF_SERVER": "server",
        "CONF_VIN": "vin",
        "CONF_AUTH_METHOD": "auth_method",
        "AUTH_METHOD_GOOGLE": "google",
        "AUTH_METHOD_EMAIL": "email",
        "CONF_XPENG_EMAIL": "xpeng_email",
        "CONF_XPENG_PASSWORD": "xpeng_password",
        "CONF_LOGIN_TOKENS": "login_tokens",
        "CONF_TENANT_DATA": "tenant_data",
        "CONF_CONFIGURED_DEVICE_ID": "configured_device_id",
        "DEFAULT_SERVER": "iot-app-global.xiaopeng.com:8885",
    }

    def _default(name: str):
        if name.startswith("CONF_") or name in ("DOMAIN",):
            return name.lower()
        if name.startswith("AUTH_METHOD_"):
            return name.split("AUTH_METHOD_")[-1].lower()
        if name.startswith("DEFAULT_"):
            return 60
        return name.lower()

    mod = types.ModuleType("custom_components.xpeng.const")

    def _getattr(name):
        return real.get(name, _default(name))

    mod.__getattr__ = _getattr  # module-level fallback for `from .const import X`
    sys.modules["custom_components.xpeng.const"] = mod
    return mod


class _FakeConfigEntries:
    def __init__(self, entry=None):
        self._entry = entry
        self.updated = None
        self.reloaded = False

    def async_get_entry(self, entry_id):
        return self._entry

    def async_update_entry(self, entry, *, data=None, **kw):
        if data:
            entry.data.update(data)
        self.updated = data

    async def async_reload(self, entry_id):
        self.reloaded = True


class _FakeHass:
    def __init__(self, entry=None):
        self.config_entries = _FakeConfigEntries(entry)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _load_pkg():
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = []
    xpkg = types.ModuleType("custom_components.xpeng")
    xpkg.__path__ = [str(PKG_DIR)]
    sys.modules["custom_components"] = pkg
    sys.modules["custom_components.xpeng"] = xpkg

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.xpeng.{name}", PKG_DIR / filename
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"custom_components.xpeng.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    return load


# ── the test ──────────────────────────────────────────────────────────────

def main() -> int:
    _install_ha_stubs()
    _install_voluptuous_stub()
    _install_const_stub()
    load = _load_pkg()

    const = sys.modules["custom_components.xpeng.const"]

    # Fake the two network-touching modules before config_flow's lazy imports.
    fake_api = types.ModuleType("custom_components.xpeng.xpeng_api")

    #: Every config dict handed to fetch_tenant, so the test can prove the flow
    #: signs as a device (the tenant is sealed against that id).
    tenant_calls = []

    def _fake_fetch_tenant(cfg=None):
        tenant_calls.append(cfg)
        return {"teeId": "tee", "_key_pool": {}, "version": 2}

    fake_api.fetch_tenant = _fake_fetch_tenant
    fake_api.mint_device_id = lambda: "0123456789abcdef0123456789abcdef"
    fake_api.create_mqtt_account = lambda cfg, tenant, tokens: {
        "clientId": "xmart:app:999:aaaa", "password": "pw", "serverSsl": "iot-app-global.xiaopeng.com:8885",
    }
    fake_api.login_get_state = lambda cfg, tenant, email: {"xid": "xid-1"}
    fake_api.login_send_code = lambda cfg, tenant, email, xid="": {"body_json": {"code": 200}}
    fake_api.login_validate_code = lambda cfg, tenant, email, code, xid="": {"xid": "xid-2"}
    fake_api.login_password = lambda cfg, tenant, email, password, xid="", **kw: {
        "uid": 999, "accessToken": "acc", "refreshToken": "ref",
    }
    fake_api.DEFAULT_APP_CONFIG = {}

    fake_gl = types.ModuleType("custom_components.xpeng.google_login")
    fake_gl.extract_code = lambda text: "THECODE" if "code=" in text or len(text) > 16 else ""
    fake_gl.build_auth_url = lambda: ("https://accounts.google.com/o/oauth2/v2/auth?client_id=…", "VERIFIER")
    fake_gl.exchange_code = lambda code, verifier: "ID_TOKEN"
    fake_gl.google_auth_login = lambda api, cfg, tenant, id_token: {
        "uid": 999, "accessToken": "acc", "refreshToken": "ref", "expiresIn": 7200, "refreshExpiresIn": 7776000,
    }
    fake_gl.decode_id_token_claims = lambda t: {"email": "driver@example.com", "aud": "…"}

    class _Err(Exception):
        pass
    fake_gl.GoogleLoginError = _Err

    sys.modules["custom_components.xpeng.xpeng_api"] = fake_api
    sys.modules["custom_components.xpeng.google_login"] = fake_gl

    cf = load("config_flow", "config_flow.py")

    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'ok' if cond else 'FAIL'}] {name}{' — ' + detail if detail and not cond else ''}")
        if not cond:
            failures.append(name)

    async def run():
        flow = cf.XPengConfigFlow()
        flow.hass = _FakeHass()

        # 1. entry point shows the chooser
        r = await flow.async_step_user()
        check("step_user shows form", r.get("type") == "form" and r.get("step_id") == "user")

        # 2. choose Google with a VIN
        r = await flow.async_step_user({const.CONF_VIN: "L1NNSGHA1SB000001", const.CONF_AUTH_METHOD: const.AUTH_METHOD_GOOGLE})
        check("google step shows form with url placeholder",
              r.get("type") == "form" and r.get("step_id") == "google"
              and "accounts.google.com" in r["description_placeholders"]["url"])

        # 3. a bad paste is rejected
        r = await flow.async_step_google({"redirect_url": "no code here"})
        check("bad paste shows invalid_code", r.get("errors", {}).get("redirect_url") == "invalid_code")

        # 4. a good paste creates the entry
        r = await flow.async_step_google({"redirect_url": f"com.googleusercontent.apps.x:/oauth2redirect?code=THECODE"})
        check("good paste creates entry", r.get("type") == "create_entry")
        data = r.get("data", {})
        check("auth_method recorded as google", data.get(const.CONF_AUTH_METHOD) == const.AUTH_METHOD_GOOGLE)
        check("no password stored", data.get(const.CONF_XPENG_PASSWORD) == "")
        check("email taken from the id token", data.get(const.CONF_XPENG_EMAIL) == "driver@example.com")
        check("mqtt client id stored", data.get(const.CONF_CLIENT_ID) == "xmart:app:999:aaaa")
        check("vin stored", data.get(const.CONF_VIN) == "L1NNSGHA1SB000001")
        check("tenant data stored", bool(data.get(const.CONF_TENANT_DATA)))

        # A tenant's key pool is sealed against the device id that fetched it
        # and the request is signed as that device, so the config dict handed to
        # fetch_tenant has to carry this flow's id — and the entry has to store
        # the same one. The REST layer reads it from the "ConfiguredDeviceId"
        # key (see xpeng_api._get_device_id); the entry stores it under
        # CONF_CONFIGURED_DEVICE_ID. The .88 copy predates the field and passes
        # no config at all; assert the id only when a config dict is passed, so
        # this file still runs against both copies.
        tenant_cfg = tenant_calls[0] if tenant_calls else None
        api_device_id = str((tenant_cfg or {}).get("ConfiguredDeviceId") or "")
        check("tenant fetch is signed as a device id",
              bool(tenant_calls) and (tenant_cfg is None or len(api_device_id) == 32))
        if tenant_cfg is None:
            print("  [i] fetch_tenant got no config — pre-device-id copy, "
                  "device-id checks skipped")
        else:
            check("entry stores the id it signed with",
                  r.get("options", {}).get(const.CONF_CONFIGURED_DEVICE_ID)
                  == api_device_id)

        # 5. reauth on a google entry goes to the google step
        entry = ConfigEntry(data={const.CONF_AUTH_METHOD: const.AUTH_METHOD_GOOGLE, const.CONF_VIN: "L1NNSGHA1SB000001"})
        flow2 = cf.XPengConfigFlow()
        flow2.hass = _FakeHass(entry)
        flow2.context["entry_id"] = entry.entry_id
        r = await flow2.async_step_reauth()
        check("google reauth shows google form", r.get("type") == "form" and r.get("step_id") == "google")
        r = await flow2.async_step_google({"redirect_url": f"x:/cb?code=THECODE"})
        check("google reauth updates, not creates", r.get("type") == "abort" and r.get("reason") == "reauth_successful")
        check("google reauth wrote fresh tokens", flow2.hass.config_entries.updated is not None)
        check("google reauth reloaded the entry", flow2.hass.config_entries.reloaded)

        # 6. an email entry still reauths via the email path
        flow3 = cf.XPengConfigFlow()
        entry2 = ConfigEntry(data={
            const.CONF_AUTH_METHOD: const.AUTH_METHOD_EMAIL,
            const.CONF_XPENG_EMAIL: "a@b.c", const.CONF_XPENG_PASSWORD: "pw", const.CONF_VIN: "VIN",
        })
        flow3.hass = _FakeHass(entry2)
        flow3.context["entry_id"] = entry2.entry_id
        r = await flow3.async_step_reauth()
        check("email reauth stays on email path", r.get("type") == "form" and r.get("step_id") == "reauth_confirm")

    asyncio.run(run())

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


class FirstScreenTests(unittest.TestCase):
    """The setup screen must not ask for what it already knows.

    Seen live (2026-10-11, screenshot from the user): the first screen was a required
    VIN box, flagged red the moment it was submitted, above a sign-in method dropdown
    whose other two options cannot complete in Home Assistant - while the renewal app
    had already put a token pair, VIN included, in pair_dir. The answer was on disk
    and the form asked for it anyway.

    These pin the shape of the fix. The behaviour was verified against a live install
    by starting a flow: it now lands on `import_tokens` with both paths filled in.
    """

    CF = (PKG_DIR / "config_flow.py").read_text(encoding="utf-8")

    def test_the_questions_are_skipped_when_a_pair_is_already_here(self):
        self.assertIn("def _pair_on_host()", self.CF)
        marker = "if user_input is None and _pair_on_host():"
        self.assertIn(marker, self.CF)
        start = self.CF.index(marker)
        self.assertIn("async_step_import_tokens()", self.CF[start:start + 200])

    def test_the_vin_is_optional_on_that_screen(self):
        """A pasted or imported pair carries its own VIN; asking twice was the bug."""
        self.assertIn('vol.Optional(CONF_VIN, default=""): str', self.CF)
        self.assertNotIn("vol.Required(CONF_VIN): str", self.CF)

    def test_a_vin_is_still_demanded_where_it_is_actually_needed(self):
        """Signing in has no pair to read one from, so that route still asks."""
        self.assertIn("if not vin and method != AUTH_METHOD_IMPORT:", self.CF)

    def test_the_dropdown_says_which_methods_cannot_complete(self):
        self.assertIn("usually blocked by the slider captcha", self.CF)

    def test_the_import_form_offers_the_pair_that_is_already_there(self):
        """Fallback for anyone who follows the older instructions anyway."""
        self.assertIn("default=default_tokens_path", self.CF)
        self.assertIn("default=default_tenant_path", self.CF)


class NotificationIntervalOptionTests(unittest.TestCase):
    """The Message Centre poll interval is the only lever on notification delay.

    Notifications are REST-only - XPENG does not push them - so a notification cannot
    arrive sooner than the interval, which used to be a bare constant.
    """

    CONST = (PKG_DIR / "const.py").read_text(encoding="utf-8")
    INIT = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    CF = (PKG_DIR / "config_flow.py").read_text(encoding="utf-8")

    def test_the_option_exists_with_a_floor(self):
        self.assertIn('CONF_NOTIFICATION_POLL_INTERVAL: Final = "notification_poll_interval"', self.CONST)
        self.assertIn("DEFAULT_NOTIFICATION_POLL_INTERVAL: Final = 300", self.CONST)
        self.assertIn("MIN_NOTIFICATION_POLL_INTERVAL: Final = 60", self.CONST)

    def test_the_timer_uses_the_option_not_the_constant(self):
        self.assertIn("timedelta(seconds=notification_poll_interval)", self.INIT)
        self.assertNotIn("timedelta(seconds=NOTIFICATION_POLL_INTERVAL)", self.INIT)
        self.assertIn("MIN_NOTIFICATION_POLL_INTERVAL,", self.INIT)

    def test_the_form_exposes_it(self):
        self.assertIn("CONF_NOTIFICATION_POLL_INTERVAL,", self.CF)
        self.assertIn("min=60, max=3600", self.CF)


class ImportStepToleranceTests(unittest.TestCase):
    """A client's wrong assumption must not leave a fresh install half-done."""

    def test_the_import_step_drops_unknown_keys_instead_of_refusing_them(self):
        """Measured on a fresh install: 0.4.16 + renewal app 0.1.14.

        Since 0.4.13 the flow opens on `import_tokens` when a pair is already on disk,
        and the app posted `{"vin": ..., "auth_method": "import"}` to whatever step came
        back. That step's schema refused it -

            400 {"errors":{"base":["not a valid option at 'auth_method'"]}}

        - so the pair was written, the entry was not created, and the install had
        credentials on disk and no entities. `REMOVE_EXTRA` accepts and drops unknown
        keys, which is the tolerant half of the fix: the app now asks which step it
        landed on, and an install that updates the integration before the app still
        finishes.
        """
        source = pathlib.Path(
            _component.module_path("config_flow.py")
        ).read_text(encoding="utf-8")
        start = source.index('step_id="import_tokens"')
        block = source[start:source.index("errors=errors", start)]
        self.assertIn("extra=vol.REMOVE_EXTRA", block)
        # the measured failure is recorded where the tolerance is, so nobody removes it
        # as noise
        self.assertIn("not a valid option at 'auth_method'", block)


if __name__ == "__main__":
    unittest.main()
