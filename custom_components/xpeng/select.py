"""Select platform for the XPENG integration."""
from __future__ import annotations

import asyncio
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

#: Seat commands are ``service_type 30`` carrying ``{"cmd_type", "mode", "cmd_value"}``.
#:
#:   cmd_type  1 = the DRIVER's seat, 0 = the passenger's. Measured on a car whose
#:             owner could check XPENG's app, because the car's own state field only
#:             says which *function* changed, not whose seat it was. These two were
#:             swapped here, which is why a left-hand-drive owner setting "passenger"
#:             heated his own seat: the values had been assigned on a right-hand-drive
#:             car, where the driver's seat is the right-hand one, and 1 was read as
#:             "passenger" from the state field that moved.
#:   cmd_value the function: 1 = heating, 2 = ventilation. 0 and 3 come back
#:             "Nicht unterstützt für dieses Fahrzeug", so neither is "off" - that is
#:             the error that owner hit pressing Off.
#:   mode      a momentary switch, NOT a level to set. Measured on the car while it
#:             reported its own fields after every command (2026-10-11): mode 1 read
#:             back 1, mode 2 read 2, mode 0 turned it off from level 2. But sending
#:             the *same* payload three times read back 1, 0, 1 - so it is a press that
#:             toggles, not an assignment, which is also why mode 3 turned the heating
#:             off rather than selecting a third level. mode 3 is therefore not
#:             offered, and `async_select_option` turns the seat off before stepping
#:             down a level so that a press cannot mean its opposite.
#:
#: Only measured combinations are offered. Guessing here moves a real seat, and the
#: car cannot always be watched while it happens.
SEAT_HEAT_OPTIONS = ["Off", "Heat 1", "Heat 2"]
SEAT_HEAT_COMMANDS = {"Off": (0, 1), "Heat 1": (1, 1), "Heat 2": (2, 1)}
SEAT_VENT_OPTIONS = ["Off", "Ventilation 1", "Ventilation 2"]
SEAT_VENT_COMMANDS = {"Off": (0, 2), "Ventilation 1": (1, 2), "Ventilation 2": (2, 2)}

#: The car reports each function in its own field - `seat.driver_seat_heat` and
#: `seat.driver_seat_ventilation` are separate - so heating and ventilation are
#: separate entities. An earlier version offered ventilation inside the heating
#: select, where the result could never appear: the heating field is truthful about
#: heat, so a ventilation command read back as Off.
#:
#: The rear seats report heat and ventilation too (`left_rear_seat_heat`, ...) and are
#: exposed as sensors, but not as controls: the cmd_type values for the rear seats have
#: not been measured, and a wrong one heats somebody.
#:
#: Steering wheel heating is a different service (31) with its own value space, which
#: has not been measured either. Left exactly as it was rather than made to look
#: consistent with the seats.
STEER_OPTIONS = ["Off", "Low", "Medium", "High"]
STEER_VALUE_MAP = {"Off": 0, "Low": 1, "Medium": 2, "High": 3}
STEER_REVERSE_MAP = {0: "Off", 1: "Low", 2: "Medium", 3: "High"}


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
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="driver_seat_heat_select",
            name="Driver seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=1,
            value_path="msg_content.instant.seat.driver_seat_heat",
            options=SEAT_HEAT_OPTIONS,
            commands=SEAT_HEAT_COMMANDS,
        ),
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="passenger_seat_heat_select",
            name="Passenger seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=0,
            value_path="msg_content.instant.seat.passenger_seat_heat",
            options=SEAT_HEAT_OPTIONS,
            commands=SEAT_HEAT_COMMANDS,
        ),
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="driver_seat_ventilation_select",
            name="Driver seat ventilation",
            icon="mdi:car-seat",
            cmd_type=1,
            value_path="msg_content.instant.seat.driver_seat_ventilation",
            options=SEAT_VENT_OPTIONS,
            commands=SEAT_VENT_COMMANDS,
        ),
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="passenger_seat_ventilation_select",
            name="Passenger seat ventilation",
            icon="mdi:car-seat",
            cmd_type=0,
            value_path="msg_content.instant.seat.passenger_seat_ventilation",
            options=SEAT_VENT_OPTIONS,
            commands=SEAT_VENT_COMMANDS,
        ),
        XPengSteeringWheelHeatSelect(coordinator, mqtt_client),
    ])


class XPengSeatSelect(XPengEntity, SelectEntity):
    """One seat function: its own state field, its own level options."""

    def __init__(
        self,
        coordinator: XPengDataCoordinator,
        mqtt_client: XPengMQTTClient,
        key: str,
        name: str,
        icon: str,
        cmd_type: int,
        value_path: str,
        options: list[str],
        commands: dict[str, tuple[int, int]],
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._cmd_type = cmd_type
        self._value_path = value_path
        self._commands = commands
        self._attr_options = options
        self._attr_unique_id = f"xpeng_{self._vin}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon

    @property
    def current_option(self) -> str | None:
        val = self._get_nested_value(self._value_path)
        if val is None:
            return None
        # The state field carries the LEVEL, and `mode` is what sets the level - not
        # `cmd_value`, which is the function and therefore the same for every option of
        # one entity. Matching on cmd_value mapped every level to whichever option came
        # first and answered Off forever; matching on mode also confirms the model, since
        # a `mode=1` command is what made the field read 1.
        for option, (mode, _cmd_value) in self._commands.items():
            if mode == val:
                return option
        # A level set from elsewhere - XPENG's app, the car's own controls - that this
        # entity does not offer yet. Unknown is the honest answer: a value outside
        # _attr_options is a warning, and "Off" would be a lie.
        return None

    async def async_select_option(self, option: str) -> None:
        mode, cmd_value = self._commands.get(option, (0, 1))

        # These are momentary switches, not level setters, which took measuring twice
        # over to see:
        #
        #   * the SAME payload sent three times read back 1, 0, 1 - a press toggles;
        #   * pressing a level while another is already on can switch the function off
        #     instead of selecting it (mode=2 straight from level 1 did exactly that).
        #
        # What IS reproducible, measured twice each on the car with its own fields read
        # back, is "off, then press the level once": mode 0 -> mode 1 reads 1, and
        # mode 0 -> mode 2 reads 2. So every level change turns the seat off first. One
        # extra command, and the result stops depending on what the car thought it was
        # already doing.
        plan = [0, mode] if mode != 0 else [0]
        for step in plan:
            await self.hass.async_add_executor_job(
                self._mqtt_client.send_command,
                self._vin, 30,
                {"cmd_type": self._cmd_type, "mode": step, "cmd_value": cmd_value},
            )
            if len(plan) > 1 and step == 0:
                # Back-to-back commands did not both land in one test, so give the car a
                # moment between the off and the press.
                await asyncio.sleep(2)

class XPengSteeringWheelHeatSelect(XPengEntity, SelectEntity):
    """Select entity for steering wheel heating level."""

    _attr_name = "Steering wheel heating"
    _attr_translation_key = "steering_wheel_heat_select"
    _attr_icon = "mdi:steering"
    _attr_options = STEER_OPTIONS

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
        return STEER_REVERSE_MAP.get(val, "Off")

    async def async_select_option(self, option: str) -> None:
        value = STEER_VALUE_MAP.get(option, 0)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 31,
            {"cmd_type": 1, "cmd_value": value},
        )
