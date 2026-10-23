"""Opt-in dashboard card registration and cleanup; dashboards are user-owned."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_DATA_KEY = "xpeng_vehicle_card"
CONF_ENABLE_VEHICLE_CARD = "enable_vehicle_card"
CARD_PATH = Path(__file__).parent / "www" / "xpeng-vehicle-card.js"
CARD_URL = "/xpeng/xpeng-vehicle-card.js"
_LEGACY_URL = "/local/xpeng-vehicle-card/xpeng-vehicle-card.js"


def _card_url() -> str:
    """Invalidate browser caches whenever the shipped JavaScript changes."""
    digest = hashlib.sha256(CARD_PATH.read_bytes()).hexdigest()[:12]
    return f"{CARD_URL}?v={digest}"


def _is_card_resource(url: str, *, legacy: bool = True) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    paths = {CARD_URL, _LEGACY_URL} if legacy else {CARD_URL}
    return not (parts.scheme or parts.netloc) and parts.path in paths


def _enabled(hass, exclude_entry_id: str | None = None) -> bool:
    """One opted-in vehicle is enough; absent options never grant consent."""
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("xpeng") if manager else ()
    return any(
        entry.entry_id != exclude_entry_id
        and entry.options.get(CONF_ENABLE_VEHICLE_CARD, False) is True
        for entry in entries
    )


def _resources(hass):
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return None
    return lovelace["resources"] if isinstance(lovelace, dict) else lovelace.resources


async def async_setup_card(hass: HomeAssistant) -> None:
    """Manage the shared resource only after an owner opts in."""
    state = hass.data.get(_DATA_KEY)
    if not _enabled(hass):
        if state and (state.get("ready") or state.get("static_registered")):
            await async_cleanup_card(hass)
        return
    resources = _resources(hass)
    if resources is None or getattr(hass, "http", None) is None:
        return

    state = hass.data.setdefault(_DATA_KEY, {})
    async with state.setdefault("lock", asyncio.Lock()):
        if state.get("ready"):
            return
        try:
            from homeassistant.components.frontend import add_extra_js_url
            from homeassistant.components.http import StaticPathConfig
            from homeassistant.components.lovelace.resources import ResourceStorageCollection

            url = await hass.async_add_executor_job(_card_url)
            if not state.get("static_registered"):
                await hass.http.async_register_static_paths([
                    StaticPathConfig(CARD_URL, str(CARD_PATH), False)
                ])
                state["static_registered"] = True

            if isinstance(resources, ResourceStorageCollection):
                await resources.async_get_info()
                matches = [item for item in resources.async_items()
                           if _is_card_resource(item.get("url", ""))]
                if matches:
                    keep = matches[0]
                    if keep["url"] != url or keep.get("type") != "module":
                        await resources.async_update_item(
                            keep["id"], {"url": url, "res_type": "module"}
                        )
                    for duplicate in matches[1:]:
                        await resources.async_delete_item(duplicate["id"])
                else:
                    await resources.async_create_item({"url": url, "res_type": "module"})
            else:
                # This never edits YAML. The options description explicitly
                # covers global loading and removing a legacy YAML resource.
                add_extra_js_url(hass, url)
                state["extra_url"] = url
            state["ready"] = True
        except Exception:  # noqa: BLE001 - optional UI must not block vehicle setup
            _LOGGER.exception("Unable to register the bundled XPENG Vehicle card")


async def async_cleanup_card(hass: HomeAssistant) -> None:
    """Stop loading our module when disabled or the last opted-in entry is removed."""
    resources = _resources(hass)
    if resources is None:
        return
    state = hass.data.setdefault(_DATA_KEY, {})
    async with state.setdefault("lock", asyncio.Lock()):
        try:
            from homeassistant.components.frontend import remove_extra_js_url
            from homeassistant.components.lovelace.resources import ResourceStorageCollection

            if isinstance(resources, ResourceStorageCollection):
                await resources.async_get_info()
                for item in list(resources.async_items()):
                    if _is_card_resource(item.get("url", ""), legacy=False):
                        await resources.async_delete_item(item["id"])
            if url := state.get("extra_url"):
                remove_extra_js_url(hass, url)
                state.pop("extra_url")
            state.pop("ready", None)
            # Static HTTP routes cannot be removed from a frozen router. This
            # route serves only public card code and disappears on HA restart.
        except Exception:  # noqa: BLE001 - removal must not strand a vehicle entry
            _LOGGER.exception("Unable to clean up the XPENG Vehicle card resource")


async def async_remove_card(hass: HomeAssistant, entry_id: str) -> None:
    """Called after config-entry removal; keep the card if another owner opted in."""
    if not _enabled(hass, exclude_entry_id=entry_id):
        await async_cleanup_card(hass)
