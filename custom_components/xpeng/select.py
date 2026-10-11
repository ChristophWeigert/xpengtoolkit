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

#: Seat commands are ``service_type 30`` with ``{"cmd_type", "mode", "cmd_value"}``.
#: Measured on a car whose responses the account holder could check in XPENG's own
#: app (2026-10-11), because the state fields alone cannot tell you which seat moved:
#:
#:   cmd_type  1 = the DRIVER's seat, 0 = the passenger's. The two were swapped here,
#:             which is why a left-hand-drive owner setting "passenger" heated his own
#:             seat: the values were assigned on a right-hand-drive car, where the
#:             driver happens to be the right-hand seat, and 1 was read as "passenger"
#:             from the state field that moved rather than from the car's own meaning.
#:   mode      the level. 1 is verified; higher levels are not, and mode 0 is the
#:             best candidate for "off" but has not been confirmed in the app.
#:   cmd_value the function: 1 = heating, 2 = ventilation. 0 and 3 come back
#:             "Nicht unterstützt für dieses Fahrzeug", so neither is "off" - that is
#:             the error the same owner hit pressing Off.
#:
#: Only the combinations below are offered: each was either measured, or is the one
#: outstanding candidate. Guessing the rest would move a real seat to no purpose.
SEAT_OPTIONS = ["Off", "Heat 1", "Ventilation 1"]
SEAT_COMMANDS = {"Off": (0, 1), "Heat 1": (1, 1), "Ventilation 1": (1, 2)}

#: The heating field the car reports, so the select can show what it will act on.
HEAT_OPTIONS = SEAT_OPTIONS
HEAT_VALUE_MAP = {0: "Off", 1: "Heat 1", 2: "Ventilation 1"}
HEAT_REVERSE_MAP = {"Off": 0, "Heat 1": 1, "Ventilation 1": 2}


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
            cmd_type=1,
            value_path="msg_content.instant.seat.driver_seat_heat",
        ),
        XPengSeatHeatSelect(
            coordinator, mqtt_client,
            key="passenger_seat_heat_select",
            name="Passenger seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=0,
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
        # `seat.driver_seat_heat` is the *heating* level. Ventilation is a different
        # function of the same seat and the car does not report it in this field, so a
        # ventilation command reads back here as Off - the field is truthful about
        # heat, which is what this entity is.
        val = self._get_nested_value(self._value_path)
        if val is None:
            return None
        return HEAT_VALUE_MAP.get(val, "Off")

    async def async_select_option(self, option: str) -> None:
        mode, cmd_value = SEAT_COMMANDS.get(option, (0, 1))
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 30,
            {"cmd_type": self._cmd_type, "mode": mode, "cmd_value": cmd_value},
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
