"""Base entity for the XPENG integration."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import XPengDataCoordinator


class XPengEntity(CoordinatorEntity[XPengDataCoordinator]):
    """Base class for all XPENG entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: XPengDataCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._vin = coordinator.vin

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
