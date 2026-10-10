"""Image platform for the XPENG integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.image import ImageEntity, Image, ImageContentTypeError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XPENG image entities."""
    coordinator: XPengDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([XPengVehicleImage(coordinator)])


def _infer_image_type(data: bytes) -> str:
    """Infer image content type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] in (b"GIF8",):
        return "image/gif"
    return "image/png"


class XPengVehicleImage(XPengEntity, ImageEntity):
    """XPENG vehicle image from the cloud vehicle-list response."""

    _attr_has_entity_name = True
    _attr_translation_key = "vehicle_picture"
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:car-side"

    def __init__(self, coordinator: XPengDataCoordinator) -> None:
        """Initialize the image entity."""
        ImageEntity.__init__(self, coordinator.hass)
        super().__init__(coordinator)
        self._attr_unique_id = f"xpeng_{self._vin}_vehicle_picture"
        self._last_image_url = coordinator.vehicle_image_url
        self._attr_image_last_updated = (
            datetime.now(timezone.utc) if coordinator.vehicle_image_url else None
        )

    @property
    def available(self) -> bool:
        """Return whether a vehicle image URL is known."""
        return bool(self.coordinator.vehicle_image_url)

    @property
    def image_url(self) -> str | None:
        """Return the vehicle picture URL."""
        return self.coordinator.vehicle_image_url

    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Load image bytes, handling XPENG/S3 images served as octet-stream."""
        if response := await self._fetch_url(url):
            header_ct = response.headers.get("content-type", "")
            if header_ct and header_ct.split("/", 1)[0].lower() == "image":
                content_type = header_ct
            else:
                content_type = _infer_image_type(response.content)
            try:
                return Image(
                    content=response.content,
                    content_type=content_type,
                )
            except (ImageContentTypeError, Exception):
                _LOGGER.warning(
                    "Vehicle image from %s has unexpected content type: %s",
                    url, header_ct,
                )
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Flush cached image bytes when the source URL changes."""
        image_url = self.coordinator.vehicle_image_url
        if image_url != self._last_image_url:
            self._cached_image = None
            self._last_image_url = image_url
            self._attr_image_last_updated = datetime.now(timezone.utc)
        super()._handle_coordinator_update()
