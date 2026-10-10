"""Calendar platform for XPENG HVAC schedules."""
from __future__ import annotations

from datetime import datetime, timedelta, time
import logging
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HVAC schedule calendar entity."""
    coordinator: XPengDataCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([XPengHvacScheduleCalendar(coordinator)])


def _parse_hhmm(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


class XPengHvacScheduleCalendar(XPengEntity, CalendarEntity):
    """Calendar showing upcoming HVAC scheduled appointments."""

    _attr_has_entity_name = True
    _attr_translation_key = "hvac_schedule_calendar"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: XPengDataCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"xpeng_{self._vin}_hvac_schedule_calendar"

    def _expand(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for item in self.coordinator.hvac_schedules:
            if not isinstance(item, dict):
                continue
            if not item.get("isValid"):
                continue
            start_t = _parse_hhmm(item.get("startTime"))
            if start_t is None:
                continue
            mask = item.get("schedule")
            mask_int = int(mask) if isinstance(mask, int) else 0
            aid = str(item.get("appointmentId") or "")
            action = item.get("actionType") or "AIR_CONDITION_OPEN"
            summary = f"HVAC {action}"
            description = (
                f"appointment_id={aid} startTime={item.get('startTime')} "
                f"cycleType={item.get('cycleType')} schedule={mask_int}"
            )

            # Walk each day in the [start, end] window
            day = start.date()
            end_day = end.date()
            while day <= end_day:
                run_dt = datetime.combine(day, start_t).replace(
                    tzinfo=dt_util.DEFAULT_TIME_ZONE
                )
                if start <= run_dt <= end:
                    # 1=Mon..7=Sun in Python's isoweekday → bit 0=Mon..bit 6=Sun
                    bit = 1 << (run_dt.isoweekday() - 1)
                    if mask_int == 0 or (mask_int & bit):
                        events.append(
                            CalendarEvent(
                                start=run_dt,
                                end=run_dt + timedelta(minutes=15),
                                summary=summary,
                                description=description,
                                uid=f"{aid}-{run_dt.date().isoformat()}",
                            )
                        )
                        if mask_int == 0:
                            # one-shot: stop after first match in window
                            break
                day += timedelta(days=1)

        events.sort(key=lambda e: e.start)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        events = self._expand(now, now + timedelta(days=14))
        return events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return self._expand(start_date, end_date)
