"""The unattended path: xpeng.import_credentials must be as strict as the card.

A scheduled renewal calls this service, so it has to validate the same way the
Repairs flow does - and it must not leak the credentials it handles.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

COMP = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "xpeng"
INIT = (COMP / "__init__.py").read_text(encoding="utf-8")
MODULE = COMP / "credential_import.py"
SOURCE = MODULE.read_text(encoding="utf-8")


def _function(name: str, source: str = SOURCE) -> ast.AST:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _keywords(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


class ServiceRegistrationTests(unittest.TestCase):
    def test_the_service_is_registered_once_in_async_setup(self):
        self.assertIn("async def async_setup(", INIT)
        self.assertIn("hass.services.async_register(", INIT)
        self.assertIn("SERVICE_IMPORT_CREDENTIALS", INIT)
        self.assertIn("has_service(DOMAIN, SERVICE_IMPORT_CREDENTIALS)", INIT)

    def test_it_returns_a_response_and_cannot_run_in_parallel_by_accident(self):
        self.assertIn("SupportsResponse.ONLY", INIT)

    def test_the_schema_accepts_both_contents_and_paths(self):
        schema = ast.parse(SOURCE)
        keys = {
            node.value
            for node in ast.walk(schema)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for key in ("tokens_json", "tokens_path", "tenant_json", "tenant_path", "entry_id", "vin"):
            self.assertIn(key, keys)

    def test_a_missing_entry_is_reported_not_ignored(self):
        self.assertIn("No XPENG config entry to import credentials into", INIT)


class ImportBehaviourTests(unittest.TestCase):
    def test_both_halves_are_required(self):
        self.assertIn('raise _TokenImportError(\n                "invalid_tokens"', SOURCE)
        self.assertIn('raise _TokenImportError(\n                "invalid_tenant"', SOURCE)

    def test_the_pair_is_validated_by_minting_mqtt_credentials(self):
        # This is what stops a stale pair from silently replacing a working one.
        self.assertIn("_import_mqtt_credentials(tokens, tenant)", SOURCE)

    def test_the_repair_card_is_cleared_and_the_entry_reloaded(self):
        self.assertIn("_clear_reauth_repair(hass, entry)", SOURCE)
        self.assertIn("async_reload(entry.entry_id)", SOURCE)

    def test_the_device_identity_follows_the_tenant(self):
        self.assertIn("configured_device_id", SOURCE)
        self.assertIn('tenant.get("deviceId")', SOURCE)

    def test_the_response_carries_no_credentials(self):
        node = _function("async_import_credentials")
        returns = [
            n for n in ast.walk(node) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
        ]
        self.assertTrue(returns, "expected a dict return")
        keys = {
            k.value for k in returns[-1].value.keys if isinstance(k, ast.Constant)
        }
        self.assertEqual(
            keys,
            {"entry_id", "vin", "device_id", "account", "client_id"},
            "the service response must not include tokens or passwords",
        )
        self.assertNotIn("password", keys)
        self.assertNotIn("tokens", keys)

    def test_the_log_line_does_not_include_the_credentials(self):
        node = _function("async_import_credentials")
        for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr in ("info", "debug", "warning"):
                text = ast.dump(call)
                self.assertNotIn("password", text)
                self.assertNotIn("CONF_LOGIN_TOKENS", text)


class HelperReuseTests(unittest.TestCase):
    def test_it_reuses_the_config_flow_helpers_rather_than_reimplementing(self):
        for helper in (
            "_import_mqtt_credentials",
            "_read_text_file",
            "_tokens_from_text",
            "_tenant_from_text",
            "_serialize_tenant",
            "_vin_from_tokens",
        ):
            self.assertIn(helper, SOURCE, f"{helper} should come from config_flow")


if __name__ == "__main__":
    unittest.main()
