"""Climate platform for the XPENG integration."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity
from .mqtt_client import XPengMQTTClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XPENG climate entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: XPengDataCoordinator = data["coordinator"]
    mqtt_client: XPengMQTTClient = data["mqtt_client"]
    async_add_entities([XPengClimate(coordinator, mqtt_client)])


class XPengClimate(XPengEntity, ClimateEntity):
    """XPENG cabin climate control."""

    _attr_name = "Climate"
    _attr_icon = "mdi:air-conditioner"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16.0
    _attr_max_temp = 32.0
    _attr_target_temperature_step = 0.5

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_cabin_climate"
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_target_temp: float | None = None
        self._optimistic_until: float = 0

    def _clear_expired_optimistic(self) -> None:
        """Clear optimistic state after timeout."""
        if time.monotonic() >= self._optimistic_until:
            self._optimistic_hvac_mode = None
            self._optimistic_target_temp = None

    def _get_hvac_value(self, key: str) -> Any:
        """Get a value from the HVAC data dict."""
        if not self.coordinator.data:
            return None
        return (
            self.coordinator.data
            .get("msg_content", {})
            .get("instant", {})
            .get("hvac", {})
            .get(key)
        )

    @property
    def current_temperature(self) -> float | None:
        """Return current cabin temperature."""
        val = self._get_hvac_value("hvac_inner_temp_float")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature(self) -> float | None:
        """Return target cabin temperature."""
        self._clear_expired_optimistic()
        if self._optimistic_target_temp is not None:
            return self._optimistic_target_temp
        val = self._get_hvac_value("hvac_temp_float")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        self._clear_expired_optimistic()
        if self._optimistic_hvac_mode is not None:
            return self._optimistic_hvac_mode
        return (
            HVACMode.HEAT_COOL
            if self._get_hvac_value("hvac_on") == 1
            else HVACMode.OFF
        )

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action based on temperatures."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        current = self.current_temperature
        target = self.target_temperature
        if current is None or target is None:
            return HVACAction.IDLE
        if current < target - 0.3:
            return HVACAction.HEATING
        if current > target + 0.3:
            return HVACAction.COOLING
        return HVACAction.IDLE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        elif hvac_mode == HVACMode.HEAT_COOL:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        """Turn climate on at current or default temperature."""
        target = self.target_temperature or 22.0
        self._optimistic_hvac_mode = HVACMode.HEAT_COOL
        self._optimistic_target_temp = target
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 3,
            {"cmd_type": 6, "cmd_value": target, "time": 15},
        )

    async def async_turn_off(self) -> None:
        """Turn climate off."""
        self._optimistic_hvac_mode = HVACMode.OFF
        self._optimistic_target_temp = None
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 3,
            {"cmd_type": 1, "cmd_value": 0, "time": 0},
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature (also turns on if off)."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        target = min(max(float(temperature), self.min_temp), self.max_temp)
        self._optimistic_hvac_mode = HVACMode.HEAT_COOL
        self._optimistic_target_temp = target
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 3,
            {"cmd_type": 6, "cmd_value": target, "time": 15},
        )
