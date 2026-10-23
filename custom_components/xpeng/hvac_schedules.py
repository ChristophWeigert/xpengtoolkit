"""Vehicle-scoped HVAC schedule actions with visible errors and refreshed state."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


def schedule_entry(hass, params: dict) -> dict:
    entries = {
        key: data
        for key, data in hass.data.get(DOMAIN, {}).items()
        if isinstance(data, dict) and data.get("coordinator") is not None
    }
    entity_id = params.get("vehicle_entity_id")
    if entity_id:
        from homeassistant.helpers import entity_registry as er

        entity = er.async_get(hass).async_get(entity_id)
        if (
            entity is None
            or entity.platform != DOMAIN
            or entity.config_entry_id not in entries
        ):
            raise HomeAssistantError(
                "Choose an entity belonging to the intended XPENG vehicle."
            )
        return entries[entity.config_entry_id]
    if len(entries) != 1:
        raise HomeAssistantError(
            "Select vehicle_entity_id when multiple XPENG vehicles are configured."
        )
    return next(iter(entries.values()))


def _check_result(result) -> None:
    if isinstance(result, dict) and (
        result.get("success") is False
        or ("code" in result and str(result["code"]) not in ("0", "200"))
    ):
        raise HomeAssistantError("XPENG rejected the HVAC schedule request.")


async def async_manage_schedule(hass, params: dict, operation: str) -> None:
    from .__init__ import _load_api_context
    from . import xpeng_api as api

    data = schedule_entry(hass, params)
    entry = data.get("entry")
    if entry is None:
        raise HomeAssistantError("No live XPENG configuration is available.")
    tokens, tenant = await hass.async_add_executor_job(_load_api_context, entry)
    if not tokens or not tenant:
        raise HomeAssistantError("XPENG API credentials are unavailable.")
    coordinator = data["coordinator"]
    zone = params.get("time_zone") or hass.config.time_zone
    prefix = (None, tenant, tokens, coordinator.vin)
    try:
        if operation in ("create", "update"):
            days = params.get("days_of_week", []) or []
            mask = sum(1 << (day - 1) for day in set(days))
            values = (
                params.get("action_type", "AIR_CONDITION_OPEN"),
                params["start_time"],
                1 if mask else 0,
                mask,
                1 if params.get("enabled", True) else 0,
                zone,
            )
            if operation == "create":
                result = await hass.async_add_executor_job(
                    api.create_aircondition_appointment, *prefix, *values, 1
                )
            else:
                result = await hass.async_add_executor_job(
                    api.update_aircondition_appointment,
                    *prefix,
                    params["appointment_id"],
                    *values,
                    "UPDATE",
                )
        elif operation == "delete":
            result = await hass.async_add_executor_job(
                api.delete_aircondition_appointment,
                *prefix,
                params["appointment_id"],
                zone,
            )
        elif operation != "list":
            raise HomeAssistantError("Unknown HVAC schedule operation.")
        if operation != "list":
            _check_result(result)
    except HomeAssistantError:
        raise
    except Exception as err:
        raise HomeAssistantError("XPENG rejected the HVAC schedule request.") from err

    try:
        result = await hass.async_add_executor_job(
            api.list_aircondition_appointments, *prefix, "AIR_CONDITION", zone
        )
        _check_result(result)
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise ValueError("Invalid schedule list")
        next_start = result.get("nextStartTime")
        coordinator.set_rest_data(
            {
                "hvac_schedules": result["list"],
                "hvac_next_start_time": (
                    next_start if isinstance(next_start, str) else None
                ),
            }
        )
    except Exception as err:
        message = (
            "The schedule request completed, but its refreshed state could not be read."
            if operation != "list"
            else "XPENG HVAC schedules could not be read."
        )
        raise HomeAssistantError(message) from err
