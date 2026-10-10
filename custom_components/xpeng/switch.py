"""Switch platform for the XPENG integration."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up XPENG switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: XPengDataCoordinator = data["coordinator"]
    mqtt_client: XPengMQTTClient = data["mqtt_client"]

    entities = [
        XPengSentinelSwitch(coordinator, mqtt_client),
        XPengTrunkPowerSwitch(coordinator, mqtt_client),
        XPengDefrostSwitch(coordinator, mqtt_client),
        XPengParkingLightsSwitch(coordinator, mqtt_client),
    ]

    # Add auto-upgrade switch if API is available
    if data.get("entry"):
        entities.append(XPengAutoUpgradeSwitch(coordinator, entry))

    async_add_entities(entities)

    # Dynamic per-schedule switches — discovered from coordinator.hvac_schedules
    known_schedule_ids: set[str] = set()

    def _discover_schedule_switches() -> None:
        new_entities: list[SwitchEntity] = []
        for item in coordinator.hvac_schedules:
            if not isinstance(item, dict):
                continue
            aid = item.get("appointmentId")
            if aid is None:
                continue
            aid_s = str(aid)
            if aid_s in known_schedule_ids:
                continue
            known_schedule_ids.add(aid_s)
            new_entities.append(
                XPengHvacScheduleSwitch(coordinator, entry, aid_s)
            )
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_discover_schedule_switches)
    _discover_schedule_switches()



class XPengSentinelSwitch(XPengEntity, SwitchEntity):
    """Switch to toggle X-Guard / sentinel (sentry) mode."""

    _attr_name = "X-Guard"
    _attr_icon = "mdi:cctv"
    _attr_assumed_state = True  # optimistic — don't bounce on stale state

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_sentinel_switch"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        if not self.coordinator.data:
            return None
        ext = (
            self.coordinator.data
            .get("msg_content", {})
            .get("instant", {})
            .get("ext", {})
        )
        if not ext:
            return None
        return ext.get("soldier_camera_status") == 1

    async def _send_and_refresh(self, msg_content: dict) -> None:
        """Send command then wake car to refresh state."""
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command, self._vin, 8, msg_content,
        )
        # Wait for car to process, then force state refresh
        await asyncio.sleep(5)
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command, self._vin, 19, {"cmd_type": 2}, 2,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._send_and_refresh({
            "action": 2,
            "settings": [
                {"code": "SENTINEL_MODE_VIDEO", "value": 1},
                {"code": "SENTINEL_MODE_SENSITIVITY", "value": 2},
            ],
        })

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._send_and_refresh({
            "action": 2,
            "settings": [
                {"code": "SENTINEL_MODE_VIDEO", "value": 0},
                {"code": "SENTINEL_MODE_SENSITIVITY", "value": 0},
            ],
        })


class XPengTrunkPowerSwitch(XPengEntity, SwitchEntity):
    """Switch to toggle trunk power outlet (V2L)."""

    _attr_name = "Trunk power"
    _attr_icon = "mdi:power-plug"
    _attr_assumed_state = True

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_trunk_power_switch"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        if not self.coordinator.data:
            return None
        tp = (
            self.coordinator.data
            .get("msg_content", {})
            .get("instant", {})
            .get("trunkPower", {})
        )
        return tp.get("powerSwitch") == 1 if tp else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 13,
            {"action": 2, "timeToPowerOffInMin": 60},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 13,
            {"action": 3, "timeToPowerOffInMin": 0},
        )


class XPengDefrostSwitch(XPengEntity, SwitchEntity):
    """Switch to toggle front defrost."""

    _attr_name = "Front defrost"
    _attr_translation_key = "defrost_switch"
    _attr_icon = "mdi:car-defrost-front"
    _attr_assumed_state = True

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_defrost_switch"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        if not self.coordinator.data:
            return None
        hvac = (
            self.coordinator.data
            .get("msg_content", {})
            .get("instant", {})
            .get("hvac", {})
        )
        return hvac.get("hvac_defrost") == 1 if hvac else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 3, {"cmd_type": 4, "cmd_value": 1},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 3, {"cmd_type": 4, "cmd_value": 0},
        )


class XPengParkingLightsSwitch(XPengEntity, SwitchEntity):
    """Switch to toggle parking lights."""

    _attr_name = "Parking lights"
    _attr_translation_key = "parking_lights_switch"
    _attr_icon = "mdi:car-parking-lights"
    _attr_assumed_state = True  # no state feedback from MQTT

    def __init__(
        self, coordinator: XPengDataCoordinator, mqtt_client: XPengMQTTClient
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_client = mqtt_client
        self._attr_unique_id = f"xpeng_{self._vin}_parking_lights_switch"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        return None  # no known MQTT state field

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 5, {"cmd_type": 4, "cmd_value": 1},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self._mqtt_client.send_command,
            self._vin, 5, {"cmd_type": 4, "cmd_value": 0},
        )


class XPengAutoUpgradeSwitch(XPengEntity, SwitchEntity):
    """Switch to toggle OTA auto-upgrade setting via REST API."""

    _attr_name = "OTA auto-upgrade"
    _attr_translation_key = "auto_upgrade_switch"
    _attr_icon = "mdi:cellphone-arrow-down"
    _attr_assumed_state = True

    def __init__(
        self, coordinator: XPengDataCoordinator, entry: Any
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"xpeng_{self._vin}_auto_upgrade_switch"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        status_data = self.coordinator.ota_auto_upgrade_status
        if not status_data:
            return None
        # status: 0=CLOSE, 1=OPEN, 2=DISPATCHING
        status = status_data.get("status")
        if status is None:
            return None
        return int(status) >= 1

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status_data = self.coordinator.ota_auto_upgrade_status
        if not status_data:
            return {}
        attrs: dict[str, Any] = {}
        if status_data.get("status") is not None:
            status_map = {0: "closed", 1: "open", 2: "dispatching"}
            attrs["raw_status"] = status_map.get(
                int(status_data["status"]), str(status_data["status"])
            )
        if status_data.get("pendingXosVersion"):
            attrs["pending_version"] = status_data["pendingXosVersion"]
        if status_data.get("osName"):
            attrs["os_name"] = status_data["osName"]
        return attrs

    async def _set_auto_upgrade(self, enabled: bool) -> None:
        """Toggle auto-upgrade via REST API."""
        from .__init__ import _load_api_context
        from .xpeng_api import set_auto_upgrade

        tokens, tenant = _load_api_context(self._entry)
        if not tokens or not tenant:
            _LOGGER.error("No API credentials for auto-upgrade toggle")
            return
        await self.hass.async_add_executor_job(
            set_auto_upgrade, None, tenant, tokens, self._vin, enabled
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._set_auto_upgrade(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._set_auto_upgrade(False)


class XPengHvacScheduleSwitch(XPengEntity, SwitchEntity):
    """One switch per HVAC scheduled appointment — enable/disable that schedule."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: XPengDataCoordinator,
        entry: Any,
        appointment_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._appointment_id = appointment_id
        self._attr_unique_id = f"xpeng_{self._vin}_hvac_schedule_{appointment_id}"
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0

    def _find_schedule(self) -> dict[str, Any] | None:
        for item in self.coordinator.hvac_schedules:
            if isinstance(item, dict) and str(item.get("appointmentId")) == self._appointment_id:
                return item
        return None

    @property
    def name(self) -> str:
        sched = self._find_schedule()
        if not sched:
            return f"HVAC schedule {self._appointment_id}"
        start = sched.get("startTime") or "?"
        mask = sched.get("schedule")
        from .sensor import _decode_schedule_bitmask
        days = _decode_schedule_bitmask(mask)
        if days:
            return f"HVAC schedule {start} ({', '.join(days)})"
        return f"HVAC schedule {start} (once)"

    @property
    def available(self) -> bool:
        return self._find_schedule() is not None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None and time.monotonic() < self._optimistic_until:
            return self._optimistic_state
        self._optimistic_state = None
        sched = self._find_schedule()
        if not sched:
            return None
        return bool(sched.get("isValid"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched = self._find_schedule()
        if not sched:
            return {"appointment_id": self._appointment_id}
        from .sensor import _decode_schedule_bitmask
        return {
            "appointment_id": self._appointment_id,
            "start_time": sched.get("startTime"),
            "cycle_type": sched.get("cycleType"),
            "days": _decode_schedule_bitmask(sched.get("schedule")),
            "schedule_bitmask": sched.get("schedule"),
            "action_type": sched.get("actionType"),
        }

    async def _update_validity(self, enabled: bool) -> None:
        sched = self._find_schedule()
        if not sched:
            _LOGGER.warning("HVAC schedule %s no longer present", self._appointment_id)
            return
        from .__init__ import _load_api_context
        from .xpeng_api import update_aircondition_appointment

        tokens, tenant = _load_api_context(self._entry)
        if not tokens or not tenant:
            _LOGGER.error("No API credentials for HVAC schedule toggle")
            return
        await self.hass.async_add_executor_job(
            update_aircondition_appointment,
            None,
            tenant,
            tokens,
            self._vin,
            self._appointment_id,
            sched.get("actionType") or "AIR_CONDITION_OPEN",
            sched.get("startTime") or "",
            int(sched.get("cycleType") or 0),
            int(sched.get("schedule") or 0),
            1 if enabled else 0,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._update_validity(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 30
        self.async_write_ha_state()
        await self._update_validity(False)
