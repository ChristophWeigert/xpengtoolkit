"""Select platform for the XPENG integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .command_policy import GROUP_COMFORT, default_policy
from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity
from .mqtt_client import XPengMQTTClient

_LOGGER = logging.getLogger(__name__)

#: Seat commands are ``service_type 30`` carrying ``{"cmd_type", "mode", "cmd_value"}``.
#:
#:   cmd_type  1 = the DRIVER's seat, 0 = the passenger's, 3 = left rear, 4 = right
#:             rear. The front two were measured against XPENG's app on a car whose
#:             owner could check it, because the car's own state field only says which
#:             *function* changed, not whose seat it was. Those two were once swapped
#:             here, which is why a left-hand-drive owner setting "passenger" heated
#:             his own seat. The rear two were measured 2026-10-14, each as a matched
#:             on/off pair on one cmd_type with the other rear seat not moving.
#:   cmd_value the function: 1 = heating, 2 = ventilation. 0 and 3 come back
#:             "Nicht unterstützt für dieses Fahrzeug", so neither is "off" - that is
#:             the error that owner hit pressing Off.
#:   mode      the LEVEL, as a selector: 0 = off, 1/2/3 = bar 1/2/3. Measured
#:             2026-10-14 (docs/SEAT_COMMANDS.md): `mode=3` from off reported 3, and
#:             `mode=0` reported 0. An earlier note in this file called `mode` a
#:             momentary press and read `mode 3` as "off" - those readings were taken
#:             about 2 s after each send, inside the car's 7-50 s apply/report window,
#:             so they showed the state from *before* the command.
#:
#: Only measured combinations are offered. Guessing here moves a real seat, and the
#: car cannot always be watched while it happens.
#:
#: "Off" is `mode 0`, which the driver and both rear seats accept. The front passenger
#: does not: `cmd_type 0, mode 0, cmd_value 1` is refused with `code 8` ("Nicht
#: unterstützt für dieses Fahrzeug"), so that one entity's Off does nothing. Nothing
#: measured turns the passenger's heating off, so it is left as it is rather than
#: guessed at - see docs/SEAT_COMMANDS.md.
SEAT_HEAT_OPTIONS = ["Off", "Heat 1", "Heat 2", "Heat 3"]
SEAT_HEAT_COMMANDS = {"Off": (0, 1), "Heat 1": (1, 1), "Heat 2": (2, 1), "Heat 3": (3, 1)}
#: Ventilation keeps two levels: a third was never measured for it, and the G6 has no
#: rear ventilation hardware at all, so `cmd_value 2` on cmd_type 3/4 is unverified.
SEAT_VENT_OPTIONS = ["Off", "Ventilation 1", "Ventilation 2"]
SEAT_VENT_COMMANDS = {"Off": (0, 2), "Ventilation 1": (1, 2), "Ventilation 2": (2, 2)}

#: The car reports each function in its own field - `seat.driver_seat_heat` and
#: `seat.driver_seat_ventilation` are separate - so heating and ventilation are
#: separate entities. An earlier version offered ventilation inside the heating
#: select, where the result could never appear: the heating field is truthful about
#: heat, so a ventilation command read back as Off.
#:
#: The rear seats report heat and ventilation too (`left_rear_seat_heat`, ...). Heating
#: is exposed for them now that their cmd_type values are measured (3 and 4), but
#: ventilation is not: the G6 has no rear ventilation hardware, so a control for it
#: would look like it worked and do nothing.
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

    policy = data.get("command_policy") or default_policy()

    entities = [
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
        # The rear seats: cmd_type 3 and 4, measured 2026-10-14. No ventilation
        # entities beside them - the G6 has no rear ventilation hardware, so the
        # cmd_value 2 case is unverified and a control for it would lie.
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="left_rear_seat_heat_select",
            name="Left rear seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=3,
            value_path="msg_content.instant.seat.left_rear_seat_heat",
            options=SEAT_HEAT_OPTIONS,
            commands=SEAT_HEAT_COMMANDS,
        ),
        XPengSeatSelect(
            coordinator, mqtt_client,
            key="right_rear_seat_heat_select",
            name="Right rear seat heating",
            icon="mdi:car-seat-heater",
            cmd_type=4,
            value_path="msg_content.instant.seat.right_rear_seat_heat",
            options=SEAT_HEAT_OPTIONS,
            commands=SEAT_HEAT_COMMANDS,
        ),
        XPengSteeringWheelHeatSelect(coordinator, mqtt_client),
    ]
    # A select's current option is state worth showing, so READ_ONLY keeps the
    # entity and only DISABLED removes it; ``_guard`` refuses the change.
    async_add_entities(
        [entity for entity in entities if entity._policy_visible(policy)]
    )


class XPengSeatSelect(XPengEntity, SelectEntity):
    """One seat function: its own state field, its own level options."""
    _policy_group = GROUP_COMFORT

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
        self._guard()
        mode, cmd_value = self._commands.get(option, (0, 1))

        # `mode` selects the level (measured 2026-10-14); it is not the momentary press
        # this originally modelled. The off-first step is kept anyway, on purpose:
        #
        #   * it is the only order measured to reproduce - `mode 0` -> `mode 1` reads 1,
        #     and every level-3 reading on the rear seats was taken from off as well;
        #   * a level change *from another level* (2 -> 3) has never been measured. The
        #     one observation of that shape - "pressing level 2 straight from level 1
        #     read 0" - is unexplained and may itself have been latency.
        #
        # So a level change still turns the seat off first. That costs one extra command
        # and makes the outcome independent of what the car thought it was doing.
        # docs/SEAT_COMMANDS.md expects this to be unnecessary; dropping it is a change
        # to make with a fresh measurement, not a cleanup.
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

    _policy_group = GROUP_COMFORT
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
        self._guard()
        value = STEER_VALUE_MAP.get(option, 0)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 31,
            {"cmd_type": 1, "cmd_value": value},
        )
