"""Button platform for the XPENG integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .command_policy import (
    ACTION_COMMAND,
    ACTION_WAKE,
    GROUP_ACCESS,
    GROUP_CHARGING,
    GROUP_LIGHTS,
    GROUP_QUERIES,
    GROUP_SECURING,
    default_policy,
)
from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity
from .mqtt_client import XPengMQTTClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XPENG button entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: XPengDataCoordinator = data["coordinator"]
    mqtt_client: XPengMQTTClient = data["mqtt_client"]

    policy = data.get("command_policy") or default_policy()

    entities = [
        XPengCommandButton(
            coordinator, mqtt_client,
            key="flash_lights",
            group=GROUP_LIGHTS,
            name="Flash lights",
            icon="mdi:car-light-high",
            service_type=5,
            msg_content={"cmd_type": 1, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="honk_horn",
            group=GROUP_LIGHTS,
            name="Honk horn",
            icon="mdi:bugle",
            service_type=6,
            msg_content={"cmd_type": 1, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="close_windows",
            group=GROUP_SECURING,
            name="Close windows",
            icon="mdi:car-door",
            service_type=4,
            msg_content={"cmd_type": 3, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="vent_windows",
            group=GROUP_ACCESS,
            name="Vent windows",
            icon="mdi:car-door",
            service_type=4,
            msg_content={"cmd_type": 3, "cmd_value": 0},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="force_update",
            group=GROUP_QUERIES,
            action=ACTION_WAKE,
            name="Force update",
            icon="mdi:refresh",
            service_type=19,
            msg_content={},
            msg_type=4,
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="wake_vehicle",
            group=GROUP_QUERIES,
            action=ACTION_WAKE,
            name="Wake vehicle",
            icon="mdi:power",
            service_type=19,
            msg_content={"cmd_type": 2},
            msg_type=2,
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="query_xguard",
            group=GROUP_QUERIES,
            name="Query X-Guard",
            icon="mdi:cctv",
            service_type=8,
            msg_content={"action": 1},
        ),
        # Trunk
        XPengCommandButton(
            coordinator, mqtt_client,
            key="open_trunk",
            group=GROUP_ACCESS,
            name="Open trunk",
            icon="mdi:car-back",
            service_type=9,
            msg_content={"cmd_type": 2, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="close_trunk",
            group=GROUP_SECURING,
            name="Close trunk",
            icon="mdi:car-back",
            service_type=9,
            msg_content={"cmd_type": 2, "cmd_value": 3},
        ),
        # Charging
        XPengCommandButton(
            coordinator, mqtt_client,
            key="start_charge",
            group=GROUP_CHARGING,
            name="Start charging",
            icon="mdi:battery-charging",
            service_type=4,
            msg_content={"cmd_type": 1, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="stop_charge",
            group=GROUP_CHARGING,
            name="Stop charging",
            icon="mdi:battery-off",
            service_type=4,
            msg_content={"cmd_type": 1, "cmd_value": 0},
        ),
        # Charge ports
        XPengCommandButton(
            coordinator, mqtt_client,
            key="open_charge_port",
            group=GROUP_CHARGING,
            name="Open charge port",
            icon="mdi:ev-plug-type2",
            service_type=4,
            msg_content={"cmd_type": 3, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="close_charge_port",
            group=GROUP_CHARGING,
            name="Close charge port",
            icon="mdi:ev-plug-type2",
            service_type=4,
            msg_content={"cmd_type": 3, "cmd_value": 0},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="open_fast_charge_port",
            group=GROUP_CHARGING,
            name="Open fast charge port",
            icon="mdi:ev-plug-ccs2",
            service_type=4,
            msg_content={"cmd_type": 4, "cmd_value": 1},
        ),
        XPengCommandButton(
            coordinator, mqtt_client,
            key="close_fast_charge_port",
            group=GROUP_CHARGING,
            name="Close fast charge port",
            icon="mdi:ev-plug-ccs2",
            service_type=4,
            msg_content={"cmd_type": 4, "cmd_value": 0},
        ),
        # Windows
        XPengCommandButton(
            coordinator, mqtt_client,
            key="open_windows",
            group=GROUP_ACCESS,
            name="Open windows",
            icon="mdi:car-door",
            service_type=12,
            msg_content={"cmd_type": 1, "cmd_value": 2},
        ),
    ]

    # A button carries no state, so READ_ONLY and DISABLED mean the same thing
    # here: the entity is simply not created. Keeping a button that raises when
    # pressed would be a worse answer than not showing it.
    async_add_entities(
        [entity for entity in entities if entity._policy_group and policy.allows(entity._policy_group)]
    )


class XPengCommandButton(XPengEntity, ButtonEntity):
    """A button that sends an MQTT command to the vehicle."""

    def __init__(
        self,
        coordinator: XPengDataCoordinator,
        mqtt_client: XPengMQTTClient,
        key: str,
        name: str,
        icon: str,
        service_type: int,
        msg_content: dict[str, Any],
        msg_type: int = 2,
        group: str | None = None,
        action: str = ACTION_COMMAND,
    ) -> None:
        super().__init__(coordinator)
        self._policy_group = group
        self._policy_action = action
        self._mqtt_client = mqtt_client
        self._service_type = service_type
        self._msg_content = msg_content
        self._msg_type = msg_type
        self._attr_unique_id = f"xpeng_{self._vin}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        self._guard(self._policy_action)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin,
            self._service_type,
            self._msg_content,
            self._msg_type,
        )
