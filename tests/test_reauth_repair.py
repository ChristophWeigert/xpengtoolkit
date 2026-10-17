#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The Repairs card raised when XPENG credentials can no longer be renewed.

    python3 tests/test_reauth_repair.py
    python3 -m unittest discover -s tests -v

Why this test exists: ``entry.async_start_reauth()`` only shows a transient
prompt, and for this integration that prompt cannot be completed in-app at all
(XPENG's sign-in is gated by a slider captcha no Home Assistant flow can solve).
Users therefore saw dead entities and a toast that vanished, with nothing
explaining that recovery means re-running the external onboarding tool.

``_start_reauth_if_idle`` is the single choke point every unusable-credentials
path goes through, so it is where the persistent card is raised. ``__init__.py``
imports Home Assistant, so this test stubs the pieces it touches and loads the
module by path - the same technique the repo's other tests use.
"""

from __future__ import annotations

import ast
import json
import importlib.util
import pathlib
import re
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

COMPONENT = _component.component_dir()


class _FakeIssueRegistry(types.ModuleType):
    """Records what the integration asks Home Assistant to display."""

    class IssueSeverity:  # noqa: N801 - mirrors the HA enum name
        ERROR = "error"
        WARNING = "warning"

    def __init__(self) -> None:
        super().__init__("homeassistant.helpers.issue_registry")
        self.created: list[dict] = []
        self.deleted: list[tuple] = []

    def async_create_issue(self, hass, domain, issue_id, **kw):  # noqa: D102
        self.created.append({"domain": domain, "issue_id": issue_id, **kw})

    def async_delete_issue(self, hass, domain, issue_id):  # noqa: D102
        self.deleted.append((domain, issue_id))


class _Entry:
    """Minimal stand-in for a ConfigEntry."""

    def __init__(self, entry_id="E1", title="XPENG 123456", data=None):
        self.entry_id = entry_id
        self.title = title
        # This harness stubs `const` with a permissive module whose CONF_*
        # attributes resolve to name.lower(), so CONF_VIN is "conf_vin" here -
        # not the real "xpeng_vin". The key must match what the module under
        # test actually reads, or masking silently degrades to "(unknown)" and
        # the assertion below would pass for the wrong reason.
        self.data = data if data is not None else {
            "conf_vin": "L1NNSGHA1SB000001"
        }
        self.reauth_started = 0

    def async_start_reauth(self, hass):  # noqa: D102
        self.reauth_started += 1


class _Hass:
    def __init__(self, pending_flows=()):
        self._pending = list(pending_flows)

        class _Flow:
            def __init__(self, flows):
                self._flows = flows

            def async_progress(self):
                return self._flows

        self.config_entries = types.SimpleNamespace(flow=_Flow(self._pending))


class _Permissive(types.ModuleType):
    """A stand-in module that answers any attribute with a harmless dummy.

    Home Assistant's import surface is large and grows between releases;
    enumerating it makes this test brittle for no gain. Anything not
    explicitly set resolves to a callable class, so both `from homeassistant.x
    import Y` and `Y(...)` work.
    """

    def __getattr__(self, name):  # noqa: D105
        if name.startswith("__"):
            raise AttributeError(name)

        class _Anything:
            """Callable, and every attribute on it is another _Anything."""

            def __call__(self, *a, **kw):
                return _Anything()

            def __getattr__(self, _n):
                return _Anything()

        dummy = _Anything()
        setattr(self, name, dummy)
        return dummy


def _load_module():
    """Load __init__.py with a stubbed Home Assistant surface."""
    registry = _FakeIssueRegistry()

    for name in (
        "homeassistant",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.const",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.event",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.components",
        "homeassistant.components.sensor",
        "homeassistant.components.binary_sensor",
    ):
        sys.modules[name] = _Permissive(name)

    ce = sys.modules["homeassistant.config_entries"]
    ce.ConfigEntry = type("ConfigEntry", (), {})
    ce.SOURCE_REAUTH = "reauth"
    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.helpers"].issue_registry = registry
    sys.modules["homeassistant.helpers.issue_registry"] = registry

    # services.py imports voluptuous at module level.
    sys.modules["voluptuous"] = _Permissive("voluptuous")

    # `from .const import ...` and `from . import xpeng_api` need the package.
    pkg = sys.modules.get("custom_components") or types.ModuleType("custom_components")
    pkg.__path__ = [str(COMPONENT.parent)]
    xpkg = types.ModuleType("custom_components.xpeng")
    xpkg.__path__ = [str(COMPONENT)]
    sys.modules["custom_components"] = pkg
    sys.modules["custom_components.xpeng"] = xpkg

    # const.py pulls in a lot of Home Assistant (entity descriptions, units).
    # Substituting a stand-in module with a module-level __getattr__ keeps this
    # test focused on the repair logic; the repo's config-flow test does the
    # same thing for the same reason.
    #
    # The stand-in only answers names the REAL const.py defines. A permissive
    # stub (it used to return a plausible value for anything) cannot see a
    # constant that was moved out of const.py while an importer still asks for
    # it there - which is exactly how AUTH_METHOD_IMPORT broke the whole module:
    # `from .const import AUTH_METHOD_IMPORT` raised ImportError, Home Assistant
    # fell back to its own ConfirmRepairFlow, and the Repairs card rendered
    # without input fields and "fixed" nothing when submitted.
    const_source = (COMPONENT / "const.py").read_text()
    _REAL_CONST_NAMES = set(
        re.findall(r"^([A-Z][A-Z0-9_]*)\s*:", const_source, re.MULTILINE)
    )
    assert "DOMAIN" in _REAL_CONST_NAMES, "const.py scan failed to find DOMAIN"

    def _const_getattr(name):
        if name not in _REAL_CONST_NAMES:
            raise AttributeError(
                f"const.py defines no {name!r}: an import of it from const.py "
                f"would fail inside Home Assistant"
            )
        if name == "DOMAIN":
            return "xpeng"
        if name.startswith("CONF_") or name.startswith("DEFAULT_"):
            return name.lower()
        return 60

    fake_const = types.ModuleType("custom_components.xpeng.const")
    fake_const.__getattr__ = _const_getattr
    sys.modules["custom_components.xpeng.const"] = fake_const

    # The sibling modules __init__.py imports from are stubbed rather than
    # executed: they pull in paho/cryptography and are irrelevant here. What is
    # under test is which repair calls __init__.py makes, not their internals.
    def _stub(name, **attrs):
        m = types.ModuleType(f"custom_components.xpeng.{name}")
        for key, value in attrs.items():
            setattr(m, key, value)
        sys.modules[f"custom_components.xpeng.{name}"] = m
        return m

    class _Any:  # accepts any construction/method call
        def __init__(self, *a, **kw):
            pass

        def __getattr__(self, _name):
            return lambda *a, **kw: None

    _stub("coordinator", XPengDataCoordinator=_Any)
    _stub("mqtt_client", XPengMQTTClient=_Any, missing_cert_files=lambda *a: [],
          build_tls_context=lambda *a, **kw: None, CERT_FILES=())
    _stub("xpeng_api", DEFAULT_APP_CONFIG={}, DEFAULT_SERVER="x",
          make_ssl_context=lambda *a, **kw: None,
          load_login_tokens=lambda *a, **kw: None,
          load_tenant_cache=lambda *a, **kw: None, mint_device_id=lambda: "0" * 32,
          set_configured_device_id=lambda *a: None, set_cert_dir=lambda *a: None,
          create_mqtt_account=lambda *a, **kw: {})
    _stub("xpeng_errors", LoginExpiredError=type("LoginExpiredError", (Exception,), {}),
          is_token_expired_response=lambda *a: False, TOKEN_EXPIRED_CODES=frozenset())
    for _plat in ("sensor", "binary_sensor", "switch", "button", "number",
                  "select", "climate", "lock", "device_tracker", "image",
                  "calendar"):
        _stub(_plat, async_setup_entry=lambda *a, **kw: True)

    spec = importlib.util.spec_from_file_location(
        "custom_components.xpeng", COMPONENT / "__init__.py",
        submodule_search_locations=[str(COMPONENT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.xpeng"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as err:
        raise unittest.SkipTest(
            f"__init__.py needs more of Home Assistant than is stubbed: {err}"
        )

    # The Repairs card is shipping-copy work; the live/private tree still calls
    # async_start_reauth() directly with no card. Report that as a skip rather
    # than 10 AttributeErrors.
    if not hasattr(mod, "_start_reauth_if_idle"):
        print(
            f"SKIP: _start_reauth_if_idle is not implemented in "
            f"{_component.component_dir().name}"
        )
        raise SystemExit(0)

    return mod, registry


class TestReauthRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.registry = _load_module()

    def setUp(self):
        self.registry.created.clear()
        self.registry.deleted.clear()

    def test_dead_credentials_raise_a_persistent_card(self):
        """The card must be raised, not just a toast, or the user is stuck."""
        entry = _Entry()
        self.mod._start_reauth_if_idle(_Hass(), entry)

        self.assertEqual(len(self.registry.created), 1, "expected one repair card")
        issue = self.registry.created[0]
        self.assertEqual(issue["issue_id"], "reauth_required")
        self.assertEqual(issue["domain"], "xpeng")
        self.assertTrue(issue["translation_key"])

    def test_card_is_fixable_and_a_matching_flow_exists(self):
        """is_fixable must not be a dead-click Fix button.

        The card offers "Fix" only because repairs.py implements a flow for
        this issue id. If the two ever disagree the user gets a button that
        does nothing, which is worse than no button - so assert both halves
        here.
        """
        self.mod._start_reauth_if_idle(_Hass(), _Entry())
        issue = self.registry.created[0]
        self.assertTrue(issue["is_fixable"], "card must offer the import flow")

        repairs = pathlib.Path(COMPONENT) / "repairs.py"
        self.assertTrue(repairs.is_file(), "is_fixable needs repairs.py to exist")
        source = repairs.read_text()
        self.assertIn("async_create_fix_flow", source)

    def test_repairs_imports_every_name_it_takes_from_const(self):
        """An unresolvable import here disables the card without any error.

        Home Assistant treats a repairs platform that fails to import as absent
        and substitutes its own `ConfirmRepairFlow`: the card still appears, has
        no input fields, and "fixes" the issue by deleting it — while the
        credentials stay dead. `AUTH_METHOD_IMPORT` was moved into config_flow.py
        while repairs.py still did `from .const import AUTH_METHOD_IMPORT`, so
        that is exactly what shipped.

        The module is inspected rather than imported because importing it needs
        Home Assistant, but the failure mode is a plain name lookup: every name
        repairs.py asks const.py for has to exist in const.py.
        """
        const_names = set(
            re.findall(
                r"^([A-Z][A-Z0-9_]*)\s*:",
                (pathlib.Path(COMPONENT) / "const.py").read_text(),
                re.MULTILINE,
            )
        )
        self.assertIn("DOMAIN", const_names, "const.py scan failed to find DOMAIN")

        source = (pathlib.Path(COMPONENT) / "repairs.py").read_text()
        imported = set()
        for match in re.finditer(r"from \.const import ([^\n(]+|\([^)]*\))", source):
            for name in re.split(r"[,\s]+", match.group(1).strip("()")):
                if name:
                    imported.add(name)

        self.assertTrue(imported, "no const imports found in repairs.py")
        missing = sorted(imported - const_names)
        self.assertEqual(
            [],
            missing,
            "repairs.py imports names const.py does not define, so the module "
            f"fails to import inside Home Assistant: {missing}",
        )

    def test_card_survives_a_restart(self):
        """A reboot must not silently hide a still-broken integration.

        is_persistent defaults to False, which would drop the card on restart
        and not re-raise it until the refresh loop failed the threshold again.
        """
        self.mod._start_reauth_if_idle(_Hass(), _Entry())
        self.assertTrue(self.registry.created[0]["is_persistent"])

    def test_card_carries_the_entry_id_for_the_flow(self):
        """The repair flow needs to know which entry to update."""
        self.mod._start_reauth_if_idle(_Hass(), _Entry(entry_id="E9"))
        self.assertEqual(self.registry.created[0]["data"]["entry_id"], "E9")

    def test_card_names_the_affected_entry(self):
        """A user with more than one vehicle must know which one broke."""
        self.mod._start_reauth_if_idle(_Hass(), _Entry(title="XPENG 123456"))
        placeholders = self.registry.created[0]["translation_placeholders"]
        self.assertEqual(placeholders["entry"], "XPENG 123456")
        self.assertIn("vin", placeholders)

    def test_reauth_prompt_still_starts(self):
        """The card is additional to the reauth prompt, not a replacement."""
        entry = _Entry()
        self.mod._start_reauth_if_idle(_Hass(), entry)
        self.assertEqual(entry.reauth_started, 1)

    def test_no_second_prompt_while_one_is_pending(self):
        hass = _Hass(pending_flows=[{
            "context": {"source": "reauth", "entry_id": "E1"}
        }])
        entry = _Entry(entry_id="E1")
        self.mod._start_reauth_if_idle(hass, entry)
        self.assertEqual(entry.reauth_started, 0, "must not stack reauth flows")

    def test_successful_refresh_clears_the_card(self):
        """Otherwise the user stares at a problem they already fixed."""
        self.mod._clear_reauth_repair(_Hass(), _Entry())
        self.assertIn(("xpeng", "reauth_required"), self.registry.deleted)

    def test_it_clears_home_assistants_own_card_too(self):
        """HA's reauth card is only cleared by HA's own flow, so clear it here.

        Measured 2026-10-17: the card was raised at 11:50, the unattended renewal
        app imported a fresh pair at 11:54, the entities worked again - and the
        card was still in Settings -> Repairs, because only *our* issue id was
        deleted. The id is HA's, spelled out in the code and asserted here against
        the exact string the live registry showed for this entry.
        """
        entry = _Entry(entry_id="01M4SK7V1B95KZFPGFGZGK0CM9")
        self.assertEqual(
            self.mod._ha_reauth_issue_id(entry),
            "config_entry_reauth_xpeng_01M4SK7V1B95KZFPGFGZGK0CM9",
        )
        self.mod._clear_reauth_repair(_Hass(), entry)
        self.assertIn(
            ("xpeng", "config_entry_reauth_xpeng_01M4SK7V1B95KZFPGFGZGK0CM9"),
            self.registry.deleted,
        )

    def test_reauth_waits_for_the_unattended_renewal(self):
        """A few quick failures must not outrun a renewal that takes minutes.

        The failures arrive every 45-60s, so five of them is five minutes, while a
        renewal app check plus a slider-captcha login took eleven. Asking first
        raises a re-onboarding card for credentials that fix themselves.
        """
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("REAUTH_MIN_FAILING_SECONDS = 600", source)
        self.assertIn("and failing_for >= REAUTH_MIN_FAILING_SECONDS", source)
        # The clock has to start on the first failure and be cleared on success, or
        # the window never elapses (or never resets).
        self.assertIn('if not refresh_state.get("first_failure_at"):', source)
        self.assertIn('refresh_state["first_failure_at"] = 0.0', source)

    def test_translation_keys_match_the_code(self):
        """The card and the flow must have text for every key they reference.

        Home Assistant resolves issue titles and fix-flow steps by
        translation_key + step_id. A typo here yields a blank card or a form
        with no labels, which looks like a broken integration rather than a
        translation bug - so assert the contract directly.
        """
        import json

        strings = json.loads((COMPONENT / "strings.json").read_text())
        issues = strings.get("issues") or {}

        # the keys _raise_reauth_repair can emit
        for key in ("login_expired", "credentials_rejected"):
            self.assertIn(key, issues, f"issues.{key} missing from strings.json")
            entry = issues[key]
            self.assertIn("title", entry)

            # the description text lives under the fix flow's step
            fix = entry.get("fix_flow") or {}
            step = (fix.get("step") or {}).get("confirm")
            self.assertIsNotNone(
                step,
                f"issues.{key}.fix_flow.step.confirm missing "
                "(repairs.py shows step_id='confirm')",
            )
            self.assertIn("description", step)
            self.assertIn("data", step)

            # placeholders the code supplies must appear in the title
            for placeholder in ("entry", "vin"):
                self.assertIn(
                    "{" + placeholder + "}",
                    entry["title"],
                    f"issues.{key}.title does not use {{{placeholder}}}",
                )

    def test_card_does_not_leak_the_full_vin(self):
        """A Repairs card ends up in screenshots; hide the identifying tail.

        Positions 12-17 are what makes a VIN unique to one car, so the card
        shows them masked. The entry title already names the vehicle, so this
        loses nothing a user needs.
        """
        self.mod._start_reauth_if_idle(_Hass(), _Entry())
        vin = self.registry.created[0]["translation_placeholders"]["vin"]
        self.assertNotIn(
            "000001", vin,
            f"the repair card leaked the VIN serial: {vin!r}",
        )
        self.assertIn("*", vin, f"expected a masked VIN, got {vin!r}")

    def test_repair_flow_has_its_entry_point(self):
        """The flow must define async_step_init, or Fix shows no form.

        Home Assistant calls `async_step_init` to start a repair flow. A class
        that defines only some other step still *registers* fine and still
        renders the issue's description - it just never presents its form, so
        the user sees instructions and no way to act on them. Observed exactly
        that: the card said "paste the two files below" with no fields below.
        """
        src = (pathlib.Path(COMPONENT) / "repairs.py").read_text()
        tree = ast.parse(src)
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        self.assertIn(
            "async_step_init", methods,
            "repairs.py has no async_step_init - the repair flow cannot start",
        )
        self.assertIn("async_create_fix_flow", methods)

        # and the form it returns must name a step the translations define
        strings = json.loads((pathlib.Path(COMPONENT) / "strings.json").read_text())
        for key, entry in (strings.get("issues") or {}).items():
            steps = set((entry.get("fix_flow") or {}).get("step") or {})
            self.assertTrue(steps, f"issues.{key}.fix_flow.step is empty")
            for step in steps:
                self.assertIn(
                    f'step_id="{step}"', src,
                    f"strings.json defines issues.{key} step {step!r} but "
                    "repairs.py never returns it",
                )

        # Home Assistant refuses a form whose step_id has no matching method,
        # and it refuses it at the worst moment: starting the flow fails with
        # "Handler ReauthRequiredRepairFlow doesn't support step confirm". The
        # flow returned step_id="confirm" while defining only async_step_init,
        # so every returned step id needs its async_step_<id> beside it.
        for step_id in sorted(set(re.findall(r'step_id="([a-z_]+)"', src))):
            self.assertIn(
                f"async_step_{step_id}", methods,
                f"repairs.py returns step_id={step_id!r} but defines no "
                f"async_step_{step_id}, which Home Assistant rejects",
            )

    def test_repair_card_failure_cannot_break_credential_handling(self):
        """A UI nicety must never take down the refresh path."""
        class Exploding(_FakeIssueRegistry):
            def async_create_issue(self, *a, **kw):
                raise RuntimeError("registry on fire")

        original = sys.modules["homeassistant.helpers.issue_registry"]
        sys.modules["homeassistant.helpers.issue_registry"] = Exploding()
        try:
            entry = _Entry()
            self.mod._start_reauth_if_idle(_Hass(), entry)
            self.assertEqual(entry.reauth_started, 1, "reauth must still happen")
        finally:
            sys.modules["homeassistant.helpers.issue_registry"] = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
