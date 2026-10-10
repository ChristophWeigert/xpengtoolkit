"""Lock platform for the XPENG integration."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    """Set up XPENG lock entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: XPengDataCoordinator = data["coordinator"]
    mqtt_client: XPengMQTTClient = data["mqtt_client"]

    async_add_entities([XPengLock(coordinator, mqtt_client)])


class XPengLock(XPengEntity, LockEntity):
    """Lock entity for the vehicle door lock."""

    _attr_name = "Door lock"
    _attr_translation_key = "door_lock"
    _attr_assumed_state = True  # optimistic — don't bounce on stale state

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_door_lock"
        self._optimistic_state: bool | None = None  # True = locked
        self._optimistic_until: float = 0

    @property
    def is_locked(self) -> bool | None:
        if (
            self._optimistic_state is not None
            and time.monotonic() < self._optimistic_until
        ):
            return self._optimistic_state
        self._optimistic_state = None
        if not self.coordinator.data:
            return None
        door = (
            self.coordinator.data
            .get("msg_content", {})
            .get("instant", {})
            .get("door", {})
        )
        val = door.get("driver_door_lock_st")
        if val is None:
            return None
        return val == 1  # 1 = locked

    async def _send_and_refresh(self, msg_content: dict) -> None:
        """Send command then wake car to refresh state."""
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command, self._vin, 9, msg_content,
        )
        # Wait for car to process, then force state refresh
        await asyncio.sleep(5)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command, self._vin, 19, {"cmd_type": 2}, 2,
        )

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the vehicle."""
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._send_and_refresh({"cmd_type": 1})

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the vehicle."""
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._send_and_refresh({"cmd_type": 0})
