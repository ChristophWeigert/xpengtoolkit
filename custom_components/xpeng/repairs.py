"""Repair flows for the XPENG integration.

The only issue this integration raises is "the credentials are dead and cannot
be renewed automatically". The recovery is always the same: re-run the external
onboarding tool, then import the fresh token + tenant pair.

Home Assistant cannot complete that recovery on its own - XPENG gates sign-in
with a slider captcha no config flow can render - but the *import* half is pure
form input, so the Repairs card offers it directly. Without this the card could
only tell the user what to do and leave them to find the right options screen.

Validation is deliberately the same code the config flow's "Import tokens" step
uses, so the two can never drift apart.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import DOMAIN, _REAUTH_ISSUE_ID, _clear_reauth_repair
from .config_flow import (
    _capacity_fields,
    _command_policy_fields,
    _entry_capacity,
    _import_mqtt_credentials,
    _read_text_file,
    _serialize_tenant,
    _tenant_from_text,
    _TokenImportError,
    _tokens_from_text,
    _submitted_option_writes,
    _vin_from_tokens,
)
from .command_policy import CommandPolicy
from .const import (
    AUTH_METHOD_IMPORT,
    CONF_AUTH_METHOD,
    CONF_CLIENT_ID,
    CONF_LOGIN_TOKENS,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_TENANT_DATA,
    CONF_VIN,
    CONF_XPENG_EMAIL,
)

_MULTILINE = TextSelector(
    TextSelectorConfig(multiline=True, type=TextSelectorType.TEXT)
)


class ReauthRequiredRepairFlow(RepairsFlow):
    """Paste a freshly minted token + tenant pair to clear the card."""

    def __init__(self) -> None:
        self._errors: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Entry point Home Assistant calls for this flow.

        The repair protocol is the two-step shape of Home Assistant's own
        ConfirmRepairFlow: `async_step_init` is the entry point, and the form it
        returns has to name a step this flow actually implements. HA 2026.10
        enforces that: a form whose `step_id` has no matching
        `async_step_<step_id>` method fails the entire flow with "Handler
        ReauthRequiredRepairFlow doesn't support step confirm", which the Repairs
        card reports as a failed fix rather than anything about credentials. The
        form therefore lives in `async_step_confirm`, and this method only hands
        the request to it.
        """
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show the token-import form, and import whatever was submitted."""
        entry = self.hass.config_entries.async_get_entry(
            str((self.data or {}).get("entry_id", ""))
        )
        if entry is None:
            # The entry is gone (user deleted it). Nothing to repair, so drop
            # the card rather than leaving it to confuse.
            ir.async_delete_issue(self.hass, DOMAIN, _REAUTH_ISSUE_ID)
            return self.async_abort(reason="entry_gone")

        if user_input is not None:
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
                mqtt = _import_mqtt_credentials(tokens, tenant)

                vin = (
                    (user_input.get(CONF_VIN) or "").strip().upper()
                    or entry.data.get(CONF_VIN, "")
                    or _vin_from_tokens(tokens)
                )
            except _TokenImportError as err:
                # Log the reason, never the payload: both files are live
                # credentials and logs get attached to support threads.
                self._errors["base"] = err.key
            except Exception:  # pragma: no cover - defensive
                self._errors["base"] = "unknown"
            else:
                client_id, password, server = mqtt
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_CLIENT_ID: client_id,
                        CONF_PASSWORD: password,
                        CONF_SERVER: server,
                        CONF_AUTH_METHOD: AUTH_METHOD_IMPORT,
                        CONF_LOGIN_TOKENS: tokens,
                        CONF_TENANT_DATA: _serialize_tenant(tenant),
                        **({CONF_VIN: vin} if vin else {}),
                        # Never blank an existing value: the older tool did not
                        # record this, and a repair should not lose it.
                        **(
                            {CONF_XPENG_EMAIL: tokens["account_email"]}
                            if tokens.get("account_email")
                            else {}
                        ),
                    },
                    # The tenant carries the device identity the pair was
                    # sealed to, so it wins over any previously stored id.
                    #
                    # This form asks the same permission and pack-size questions
                    # the setup screen does, so they are applied here as well -
                    # only the answers it actually carried, so the card cannot
                    # narrow an install it was only meant to re-credential.
                    options={
                        **entry.options,
                        "configured_device_id": str(
                            tenant.get("deviceId") or ""
                        ).strip(),
                        **_submitted_option_writes(user_input),
                    },
                )
                _clear_reauth_repair(self.hass, entry)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Optional("tokens_json", default=""): _MULTILINE,
                vol.Optional("tokens_path", default=""): str,
                vol.Optional("tenant_json", default=""): _MULTILINE,
                vol.Optional("tenant_path", default=""): str,
                # Pre-filled from the entry: this card is a recovery path, and
                # the last thing it should do is change a setting the user did
                # not come here to change.
                **_command_policy_fields(
                    CommandPolicy.from_options(entry.options)
                ),
                **_capacity_fields(
                    _entry_capacity(entry)
                ),
            }
        )
        return self.async_show_form(
            step_id="confirm", data_schema=schema, errors=self._errors
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the flow for our issue."""
    return ReauthRequiredRepairFlow()
