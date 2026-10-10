"""Select platform for the XPENG integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity
from .mqtt_client import XPengMQTTClient

_LOGGER = logging.getLogger(__name__)

HEAT_OPTIONS = ["Off", "Low", "Medium", "High"]
HEAT_VALUE_MAP = {"Off": 0, "Low": 1, "Medium": 2, "High": 3}
HEAT_REVERSE_MAP = {0: "Off", 1: "Low", 2: "Medium", 3: "High"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XPENG select entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: XPengDataCoordinator = data["coordinator"]
    mqtt_client: XPengMQTTClient = data["mqtt_client"]

    async_add_entities([
        XPengSeatHeatSelect(
            coordinator, mqtt_client,
            key="driver_seat_heat_select",
            name="Driver seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=0,
            value_path="msg_content.instant.seat.driver_seat_heat",
        ),
        XPengSeatHeatSelect(
            coordinator, mqtt_client,
            key="passenger_seat_heat_select",
            name="Passenger seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=1,
            value_path="msg_content.instant.seat.passenger_seat_heat",
        ),
        XPengSteeringWheelHeatSelect(coordinator, mqtt_client),
    ])


class XPengSeatHeatSelect(XPengEntity, SelectEntity):
    """Select entity for seat heating level."""

    _attr_options = HEAT_OPTIONS

    def __init__(
        self,
        coordinator: XPengDataCoordinator,
        mqtt_client: XPengMQTTClient,
        key: str,
        name: str,
        icon: str,
        cmd_type: int,
        value_path: str,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._cmd_type = cmd_type
        self._value_path = value_path
        self._attr_unique_id = f"xpeng_{self._vin}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon

    @property
    def current_option(self) -> str | None:
        val = self._get_nested_value(self._value_path)
        if val is None:
            return None
        return HEAT_REVERSE_MAP.get(val, "Off")

    async def async_select_option(self, option: str) -> None:
        value = HEAT_VALUE_MAP.get(option, 0)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 30,
            {"cmd_type": self._cmd_type, "mode": 1, "cmd_value": value},
        )


class XPengSteeringWheelHeatSelect(XPengEntity, SelectEntity):
    """Select entity for steering wheel heating level."""

    _attr_name = "Steering wheel heating"
    _attr_translation_key = "steering_wheel_heat_select"
    _attr_icon = "mdi:steering"
    _attr_options = HEAT_OPTIONS

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_steering_wheel_heat_select"

    @property
    def current_option(self) -> str | None:
        val = self._get_nested_value(
            "msg_content.instant.steerWheel.steering_wheel_heat"
        )
        if val is None:
            return None
        return HEAT_REVERSE_MAP.get(val, "Off")

    async def async_select_option(self, option: str) -> None:
        value = HEAT_VALUE_MAP.get(option, 0)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 31,
            {"cmd_type": 1, "cmd_value": value},
        )
