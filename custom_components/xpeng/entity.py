"""Base entity for the XPENG integration."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .command_gate import guard_entity
from .command_policy import (
    ACTION_COMMAND,
    ACTION_WAKE,
    CommandPolicy,
    default_policy,
)
from .const import DOMAIN
from .coordinator import XPengDataCoordinator


class XPengEntity(CoordinatorEntity[XPengDataCoordinator]):
    """Base class for all XPENG entities."""

    _attr_has_entity_name = True

    #: Capability group (command_policy.py) that governs this entity's default
    #: command. ``None`` means the entity issues no command at all - which is
    #: every entity on the sensor, binary_sensor, device_tracker, calendar and
    #: image platforms, none of which are gated.
    _policy_group: str | None = None
    #: Groups that make this entity worth creating. Defaults to
    #: ``_policy_group``; only entities whose command spans two groups (the
    #: door lock, X-Guard) need to say so explicitly, because they must still
    #: exist when only one of the two is exposed.
    _policy_groups: tuple[str, ...] = ()

    def __init__(self, coordinator: XPengDataCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._vin = coordinator.vin

    @property
    def _policy(self) -> CommandPolicy:
        """This entry's command policy.

        Held on the coordinator because every command entity already has one,
        and an entity command method has no convenient way to reach its config
        entry. Falls back to FULL, which is also what an entry with no stored
        policy resolves to.
        """
        return getattr(self.coordinator, "command_policy", None) or default_policy()

    def _policy_visible(self, policy: CommandPolicy) -> bool:
        """Should this entity be created at all under ``policy``?"""
        groups = self._policy_groups or (
            (self._policy_group,) if self._policy_group else ()
        )
        if not groups:
            return True
        return any(policy.exposes(group) for group in groups)

    def _guard(
        self, action: str = ACTION_COMMAND, group: str | None = None
    ) -> None:
        """Refuse a command the entry's permissions do not allow.

        Raises ``HomeAssistantError`` so the refusal shows up in the UI and in
        an automation trace, instead of the command quietly not happening.
        """
        group = group or self._policy_group
        if group is None:
            return
        guard_entity(
            self.hass,
            self.entity_id or self.unique_id or self.name or "xpeng entity",
            group,
            self._policy,
            action,
        )

    def _guard_wake(self, group: str | None = None) -> None:
        """Guard a command that also wakes the car (see ``allow_wake``)."""
        self._guard(ACTION_WAKE, group)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to group all entities under one device.

        ``model`` is derived rather than hardcoded: the manufacturer's reported
        ``seriesName`` wins, then the VIN-derived model, then the VIN model
        family. A XPENG VIN only encodes the model *family* (positions 4-8), so
        trim is never claimed here — see ``vin_descriptor.py``.
        """
        info = self.coordinator.vin_info
        model = (
            self.coordinator.series_name
            or (info.model_name if info is not None else None)
            or (info.model_family if info is not None else None)
            or "XPENG"
        )
        device = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            name=f"XPENG {self._vin[-6:]}",
            manufacturer="XPENG",
            model=model,
        )
        if info is not None and info.model_family:
            device["model_id"] = info.model_family
        return device

    def _get_nested_value(self, path: str) -> Any:
        """Get a nested value from coordinator data using dot notation.

        Example path: "msg_content.instant.charge.battery_soc"
        """
        if not self.coordinator.data:
            return None
        keys = path.split(".")
        val: Any = self.coordinator.data
        for key in keys:
            if isinstance(val, dict) and key in val:
                val = val[key]
            else:
                return None
        return val
