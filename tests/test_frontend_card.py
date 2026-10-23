"""Exercise bundled-card install, upgrades and migration with no vehicle access."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "custom_components/xpeng/frontend.py"
spec = importlib.util.spec_from_file_location("xpeng_card_frontend", FILE)
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)


class StorageResources:
    def __init__(self, items=()):
        self.items = [dict(item) for item in items]
        self.loaded = False
        self.writes = []

    async def async_get_info(self):
        self.loaded = True
        return {"resources": len(self.items)}

    def async_items(self):
        assert self.loaded, "Resources must be loaded before reading them"
        return self.items

    async def async_create_item(self, data):
        assert self.loaded
        item = {"id": "new-card", "url": data["url"], "type": data["res_type"]}
        self.items.append(item)
        self.writes.append(("create", data))
        return item

    async def async_update_item(self, item_id, data):
        assert self.loaded
        item = next(item for item in self.items if item["id"] == item_id)
        item.update(url=data["url"], type=data["res_type"])
        self.writes.append(("update", item_id))

    async def async_delete_item(self, item_id):
        self.items[:] = [item for item in self.items if item["id"] != item_id]
        self.writes.append(("delete", item_id))


@dataclass
class StaticPathConfig:
    url_path: str
    path: str
    cache_headers: bool


class CardRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.extra = []
        modules = {}
        for name in (
            "homeassistant", "homeassistant.components",
            "homeassistant.components.frontend", "homeassistant.components.http",
            "homeassistant.components.lovelace",
            "homeassistant.components.lovelace.resources",
        ):
            modules[name] = ModuleType(name)
        modules["homeassistant.components.frontend"].add_extra_js_url = (
            lambda hass, url: self.extra.append(url)
        )
        modules["homeassistant.components.frontend"].remove_extra_js_url = lambda hass, url: self.extra.remove(url)
        modules["homeassistant.components.http"].StaticPathConfig = StaticPathConfig
        modules["homeassistant.components.lovelace.resources"].ResourceStorageCollection = StorageResources
        self.patcher = patch.dict(sys.modules, modules)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def hass(self, resources, *, legacy=False, enabled=True):
        async def executor(fn):
            return fn()
        lovelace = {"resources": resources} if legacy else SimpleNamespace(resources=resources)
        return SimpleNamespace(
            data={"lovelace": lovelace},
            config_entries=SimpleNamespace(async_entries=lambda domain: [SimpleNamespace(entry_id="car", options={"enable_vehicle_card": enabled})]),
            http=SimpleNamespace(async_register_static_paths=AsyncMock()),
            async_add_executor_job=executor,
        )

    async def test_default_does_not_install_or_migrate_anything(self):
        old = {"id": "manual", "url": "/local/xpeng-vehicle-card/xpeng-vehicle-card.js", "type": "module"}
        resources = StorageResources([old])
        hass = self.hass(resources, enabled=False)
        await frontend.async_setup_card(hass)
        self.assertEqual(resources.items, [old])
        self.assertFalse(resources.loaded)
        hass.http.async_register_static_paths.assert_not_awaited()
        self.assertEqual(self.extra, [])

    async def test_disabling_cleans_only_managed_resource(self):
        other = {"id": "vacuum", "url": "/local/vacuum.js", "type": "module"}
        resources = StorageResources([other])
        hass = self.hass(resources)
        await frontend.async_setup_card(hass)
        hass.config_entries.async_entries = lambda domain: []
        await frontend.async_setup_card(hass)
        self.assertEqual(resources.items, [other])

    async def test_removing_one_vehicle_keeps_resource_for_another_opted_in_vehicle(self):
        resources = StorageResources()
        hass = self.hass(resources)
        await frontend.async_setup_card(hass)
        hass.config_entries.async_entries = lambda domain: [
            SimpleNamespace(entry_id="second", options={"enable_vehicle_card": True})
        ]
        await frontend.async_remove_card(hass, "car")
        self.assertEqual(len(resources.items), 1)
        await frontend.async_remove_card(hass, "second")
        self.assertEqual(resources.items, [])

    async def test_yaml_cleanup_stops_global_module_loading(self):
        hass = self.hass(SimpleNamespace(data=[]))
        await frontend.async_setup_card(hass)
        self.assertEqual(len(self.extra), 1)
        await frontend.async_remove_card(hass, "car")
        self.assertEqual(self.extra, [])

    async def test_clean_install_ships_and_registers_one_module(self):
        resources = StorageResources()
        hass = self.hass(resources)
        await frontend.async_setup_card(hass)
        self.assertEqual(len(resources.items), 1)
        self.assertEqual(resources.items[0]["type"], "module")
        digest = hashlib.sha256(frontend.CARD_PATH.read_bytes()).hexdigest()[:12]
        self.assertEqual(resources.items[0]["url"], f"{frontend.CARD_URL}?v={digest}")
        route = hass.http.async_register_static_paths.call_args.args[0][0]
        self.assertEqual(Path(route.path), frontend.CARD_PATH)
        self.assertEqual(route.url_path, frontend.CARD_URL)
        self.assertFalse(route.cache_headers)
        self.assertEqual(self.extra, [])

    async def test_legacy_resource_migrates_without_changing_its_id_or_others(self):
        other = {"id": "vacuum", "url": "/local/vacuum.js", "type": "module"}
        resources = StorageResources([
            other, {"id": "existing", "url": "/local/xpeng-vehicle-card/xpeng-vehicle-card.js?v=0.1.6", "type": "module"}
        ])
        await frontend.async_setup_card(self.hass(resources))
        self.assertEqual(resources.items[0], other)
        self.assertEqual(resources.items[1]["id"], "existing")
        self.assertTrue(resources.items[1]["url"].startswith(frontend.CARD_URL + "?v="))
        self.assertEqual(resources.writes, [("update", "existing")])

    async def test_duplicate_old_and_new_resources_collapse_to_one(self):
        resources = StorageResources([
            {"id": "old", "url": "/local/xpeng-vehicle-card/xpeng-vehicle-card.js?x=1", "type": "js"},
            {"id": "duplicate", "url": frontend.CARD_URL + "?v=stale", "type": "module"},
        ])
        await frontend.async_setup_card(self.hass(resources))
        self.assertEqual([item["id"] for item in resources.items], ["old"])
        self.assertEqual(resources.items[0]["type"], "module")

    async def test_similarly_named_and_external_resources_are_not_migrated(self):
        others = [
            {"id": "external", "url": "https://example.com" + frontend.CARD_URL, "type": "module"},
            {"id": "similar", "url": frontend.CARD_URL + ".custom", "type": "module"},
        ]
        resources = StorageResources(others)
        await frontend.async_setup_card(self.hass(resources))
        self.assertEqual(resources.items[:2], others)
        self.assertEqual(len(resources.items), 3)

    async def test_multiple_cars_and_reload_register_once(self):
        resources = StorageResources()
        hass = self.hass(resources)
        await asyncio.gather(*(frontend.async_setup_card(hass) for _ in range(3)))
        await frontend.async_setup_card(hass)
        hass.http.async_register_static_paths.assert_awaited_once()
        self.assertEqual(len(resources.items), 1)
        self.assertEqual(len(resources.writes), 1)

    async def test_existing_current_resource_is_not_rewritten(self):
        resources = StorageResources([{"id": "current", "url": frontend._card_url(), "type": "module"}])
        await frontend.async_setup_card(self.hass(resources))
        self.assertEqual(resources.writes, [])

    async def test_upgrade_replaces_stale_cache_url(self):
        resources = StorageResources([{"id": "current", "url": frontend.CARD_URL + "?v=old", "type": "module"}])
        await frontend.async_setup_card(self.hass(resources))
        self.assertEqual(resources.items[0]["url"], frontend._card_url())
        self.assertEqual(resources.items[0]["id"], "current")

    async def test_yaml_resources_are_untouched_and_module_autoloads(self):
        items = [{"url": "/local/another-card.js", "type": "module"}]
        yaml = SimpleNamespace(data=items)
        await frontend.async_setup_card(self.hass(yaml))
        self.assertEqual(yaml.data, items)
        self.assertEqual(self.extra, [frontend._card_url()])

    async def test_old_home_assistant_dict_layout(self):
        resources = StorageResources()
        await frontend.async_setup_card(self.hass(resources, legacy=True))
        self.assertEqual(len(resources.items), 1)

    async def test_headless_home_assistant_skips_frontend(self):
        await frontend.async_setup_card(SimpleNamespace(data={}))
        await frontend.async_setup_card(SimpleNamespace(data={"lovelace": object()}))

    async def test_optional_frontend_failure_does_not_abort_and_can_retry(self):
        resources = StorageResources()
        hass = self.hass(resources)
        hass.http.async_register_static_paths.side_effect = OSError("unavailable")
        with self.assertLogs(frontend._LOGGER, level="ERROR"):
            await frontend.async_setup_card(hass)
        self.assertEqual(resources.items, [])
        hass.http.async_register_static_paths.side_effect = None
        await frontend.async_setup_card(hass)
        self.assertEqual(len(resources.items), 1)

    async def test_resource_failure_retries_without_registering_static_twice(self):
        resources = StorageResources()
        hass = self.hass(resources)
        with patch.object(resources, "async_create_item", side_effect=OSError("storage unavailable")):
            with self.assertLogs(frontend._LOGGER, level="ERROR"):
                await frontend.async_setup_card(hass)
        await frontend.async_setup_card(hass)
        hass.http.async_register_static_paths.assert_awaited_once()
        self.assertEqual(len(resources.items), 1)


if __name__ == "__main__":
    unittest.main()
