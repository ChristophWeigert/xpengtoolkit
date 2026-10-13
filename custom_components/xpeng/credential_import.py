"""Import a token + tenant pair into an existing config entry.

The Repairs card and the ``xpeng.import_credentials`` service do the same job, and
both must do it the same way: validate the pair by minting MQTT credentials, refuse
a mismatched tenant, adopt the device identity the tenant carries, then reload. A
renewal script running on a timer must not have a weaker path than a human clicking
Fix, so the validation helpers are the ones the repair flow already uses.

The credentials themselves are never logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant

from . import _clear_reauth_repair
from .config_flow import (
    _import_mqtt_credentials,
    _read_text_file,
    _serialize_tenant,
    _tenant_from_text,
    _TokenImportError,
    _tokens_from_text,
    _vin_from_tokens,
)
from .const import (
    AUTH_METHOD_IMPORT,
    CONF_AUTH_METHOD,
    CONF_CLIENT_ID,
    CONF_LAST_RENEWAL,
    CONF_LOGIN_TOKENS,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_TENANT_DATA,
    CONF_VIN,
    CONF_XPENG_EMAIL,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_CREDENTIALS = "import_credentials"

#: Same shape as the repair form: contents or a path for each half of the pair.
IMPORT_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("tokens_json"): str,
        vol.Optional("tokens_path"): str,
        vol.Optional("tenant_json"): str,
        vol.Optional("tenant_path"): str,
        vol.Optional("vin"): str,
    }
)


async def async_import_credentials(
    hass: HomeAssistant,
    entry: Any,
    *,
    tokens_json: str = "",
    tokens_path: str = "",
    tenant_json: str = "",
    tenant_path: str = "",
    vin: str = "",
) -> dict[str, Any]:
    """Validate a pair and swap it into ``entry``, then reload.

    Raises ``_TokenImportError`` (``.key`` names the failure) so both callers can
    report a reason without touching the payload.
    """
    tokens_text = (tokens_json or "").strip()
    tenant_text = (tenant_json or "").strip()

    if not tokens_text:
        path = (tokens_path or "").strip()
        if not path:
            raise _TokenImportError(
                "invalid_tokens", "no token file contents or path was given"
            )
        tokens_text = await hass.async_add_executor_job(
            _read_text_file, path, "invalid_tokens_path"
        )
    if not tenant_text:
        path = (tenant_path or "").strip()
        if not path:
            raise _TokenImportError(
                "invalid_tenant", "no tenant file contents or path was given"
            )
        tenant_text = await hass.async_add_executor_job(
            _read_text_file, path, "invalid_tenant_path"
        )

    tokens = _tokens_from_text(tokens_text)
    tenant = _tenant_from_text(tenant_text)
    # Mints MQTT credentials against the live API: this is what proves the pair is
    # current and belongs together, so a stale file cannot silently replace a
    # working one.
    client_id, password, server = _import_mqtt_credentials(tokens, tenant)

    resolved_vin = (
        (vin or "").strip().upper()
        or entry.data.get(CONF_VIN, "")
        or _vin_from_tokens(tokens)
    )
    device_id = str(tenant.get("deviceId") or "").strip()

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CLIENT_ID: client_id,
            CONF_PASSWORD: password,
            CONF_SERVER: server,
            CONF_AUTH_METHOD: AUTH_METHOD_IMPORT,
            CONF_LOGIN_TOKENS: tokens,
            # The clock behind sensor.xpeng_<car>_last_renewal. This function is the
            # app's renewal path (and the Repairs card's), so a renewal is exactly
            # what this stamp records - see the sensor for why it matters.
            CONF_LAST_RENEWAL: time.time(),
            CONF_TENANT_DATA: _serialize_tenant(tenant),
            **({CONF_VIN: resolved_vin} if resolved_vin else {}),
            **(
                {CONF_XPENG_EMAIL: tokens["account_email"]}
                if tokens.get("account_email")
                else {}
            ),
        },
        # The tenant carries the device identity the pair was sealed to, so it wins
        # over any previously stored id.
        options={**entry.options, "configured_device_id": device_id},
    )
    _clear_reauth_repair(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)

    _LOGGER.info(
        "Imported credentials for %s (device %s) - reloaded",
        resolved_vin or entry.title,
        device_id[:8] + "..." if device_id else "unknown",
    )
    return {
        "entry_id": entry.entry_id,
        "vin": resolved_vin,
        "device_id": device_id,
        "account": tokens.get("account_email"),
        "client_id": client_id,
    }


__all__ = [
    "IMPORT_CREDENTIALS_SCHEMA",
    "SERVICE_IMPORT_CREDENTIALS",
    "async_import_credentials",
]
