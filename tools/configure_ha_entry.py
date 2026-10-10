#!/usr/bin/env python3
"""Finish onboarding: produce (or install) the HA config entry for a vehicle.

Run this AFTER tools/onboard_email_login.py has written xpeng_tokens.json and
xpeng_tenant.json. It reads the VIN and uid out of those tokens, so nothing has to
be typed by hand.

Why this exists: HA's own config flow cannot be used with onboarding tokens. Its
email+password step calls login_send_code() and demands a fresh emailed
verification code, so pointing it at a perfectly good token file still dead-ends.

Two modes:

  (default) print the entry + exact UI instructions.
      Everything needed to add the integration through Settings -> Devices &
      Services is printed, including the values to type and the file paths.

  --config-dir DIR
      Write the entry straight into DIR/.storage/core.config_entries, the way a
      direct install does when the HA config is reachable on this machine.
      Requires DIR to be the HA configuration directory (the one containing
      .storage/ and configuration.yaml), and a restart afterwards.

Usage:
    # print what to do (works for any install)
    /usr/bin/python3 tools/configure_ha_entry.py

    # or install directly, then restart HA
    /usr/bin/python3 tools/configure_ha_entry.py --config-dir /config
    /usr/bin/python3 tools/configure_ha_entry.py --config-dir /var/lib/docker/volumes/hass_config/_data
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import pathlib
import secrets
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMP = ROOT / "custom_components" / "xpeng"
TOKENS = COMP / "xpeng_tokens.json"
TENANT = COMP / "xpeng_tenant.json"

DEFAULT_SERVER = "iot-app-global.xiaopeng.com:8885"
# Defaults mirror const.py; the entry itself stores the resolved values.
DEFAULTS = {
    "scan_interval": 60,
    "parked_scan_interval": 3600,
    "rest_scan_interval": 300,
    "mqtt_refresh_interval": 45,
    "mqtt_listener_enabled": False,
    "mqtt_listener_topics": "",
    "mqtt_topic_discovery": False,
    "usable_battery_kwh": 87.5,
}


def load(name: str, path: pathlib.Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fetch_vin(api, tokens, tenant, override: str | None) -> str:
    """
    The VIN, from --vin or from the live account.

    The tenant file stores no VIN (only deviceId/teeId/secrets), so it has to be
    read from the account's vehicle list. That call needs no MQTT credentials -
    just the token pair this script was pointed at.
    """
    if override:
        return override.strip().upper()
    try:
        vehicles = api.get_vehicles(None, tenant, tokens)
    except Exception as exc:
        raise SystemExit(f"could not fetch the vehicle list ({exc}); pass --vin")
    items = (vehicles or {}).get("vehicles") or []
    vins = [v.get("vin") for v in items if isinstance(v, dict) and v.get("vin")]
    if len(vins) == 1:
        return vins[0].upper()
    if not vins:
        raise SystemExit("the account has no vehicles; pass --vin")
    raise SystemExit(f"the account has {len(vins)} vehicles ({', '.join(vins)}); "
                     f"pass --vin to choose one")


def build_entry(vin: str, tokens, tenant, config_dir: str) -> dict:
    """The config entry, in exactly the shape HA stores in .storage."""
    uid = tokens.get("uid")
    if not uid:
        raise SystemExit("tokens have no uid - re-run onboard_email_login.py")
    device_id = tenant.get("deviceId")
    if not device_id:
        raise SystemExit("tenant has no deviceId - it must come from the same run as the tokens")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    token_file = f"{config_dir.rstrip('/')}/custom_components/xpeng/xpeng_tokens.json"
    tenant_file = f"{config_dir.rstrip('/')}/custom_components/xpeng/xpeng_tenant.json"

    entry = {
        "created_at": now,
        "data": {
            "client_id": f"xmart:app:{uid}:{device_id}",
            "password": "",  # minted from the tokens at runtime
            "server": DEFAULT_SERVER,
            "vin": vin,
        },
        "disabled_by": None,
        "discovery_keys": {},
        "domain": "xpeng",
        "entry_id": secrets.token_hex(6).upper(),
        "minor_version": 1,
        "modified_at": now,
        "options": {
            "configured_device_id": device_id,
            "token_file": token_file,
            "tenant_file": tenant_file,
            "iccid": "",
            "iphone_ip": "",
            "ssh_password": "",
            "mac_ip": "",
            "mac_user": "",
            "mac_password": "",
            **DEFAULTS,
        },
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "subentries": {},
        "title": f"XPENG {vin[-6:]}" if vin else "XPENG",
        "unique_id": vin,
        "version": 1,
    }
    return entry


def print_instructions(entry: dict) -> None:
    vin = entry["unique_id"]
    dev = entry["options"]["configured_device_id"]
    print()
    print("=" * 72)
    print("HOW TO FINISH (Home Assistant)")
    print("=" * 72)
    print()
    print("1. Copy the integration into your HA config directory:")
    print("     cp -r custom_components/xpeng   <config>/custom_components/")
    print("   (xpeng_tokens.json and xpeng_tenant.json ship inside it - both needed,")
    print("    they are a matched pair sealed with the same tenant key pool.)")
    print()
    print("2. Settings -> Devices & Services -> Add Integration -> XPENG")
    print(f"     VIN            : {vin}")
    print("     Auth method    : Email + password")
    print()
    print("   NOTE: do NOT complete the email-code step it offers. It calls")
    print("   login_send_code() and wants a freshly emailed code, so it cannot use")
    print("   the tokens you already have. Cancel it after the entry exists.")
    print()
    print("3. Open the XPENG entry -> Configure, and set:")
    print(f"     token_file     : {entry['options']['token_file']}")
    print(f"     tenant_file    : {entry['options']['tenant_file']}")
    print()
    print("4. Restart Home Assistant (or reload the entry).")
    print()
    print("   Expect ONE 'MQTT connection refused: rc=Not authorized' on first")
    print("   start: the entry's stored password is intentionally empty and the")
    print("   integration mints a real one via the API, then reconnects.")
    print()
    print("=" * 72)
    print("REFERENCE - the exact entry HA will store (for support/debugging)")
    print("=" * 72)
    print(f"  unique_id (VIN)      : {vin}")
    print(f"  configured_device_id : {dev}")
    print(f"  client_id            : {entry['data']['client_id']}")
    print(f"  server               : {entry['data']['server']}")
    print(f"  token_file           : {entry['options']['token_file']}")
    print(f"  tenant_file          : {entry['options']['tenant_file']}")
    print()
    print("  Raw entry JSON (what --config-dir would install):")
    print(json.dumps(entry, indent=2))


def last_vehicle_position(ha_url: str, token: str, entity_id: str):
    """
    The car's current lat/lon from Home Assistant, best effort.

    Used only to detect a default Home zone; if anything is missing we skip the
    check rather than guess.
    """
    if not (ha_url and token):
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{ha_url.rstrip('/')}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            attrs = json.loads(resp.read().decode()).get("attributes", {})
        return (float(attrs["latitude"]), float(attrs["longitude"]))
    except Exception:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def ensure_home_zone(config_dir: pathlib.Path, vehicle: tuple[float, float] | None) -> None:
    """
    Point zone.home at the vehicle, when the instance still has the default.

    A freshly installed HA has the onboarding-default lat/lon, which can easily be
    hundreds of metres from the house. The car then reads as `away` while sitting on
    the driveway, because HA decides home/away purely by distance to zone.home.
    Since onboarding is normally run with the car parked at home, the vehicle's own
    GPS is the best available anchor.

    Only applied when the existing anchor is clearly wrong (>500 m), so a
    deliberately configured home is never overwritten.
    """
    if not vehicle:
        return
    cfg_path = config_dir / ".storage" / "core.config"
    if not cfg_path.is_file():
        print("[i] no .storage/core.config - set your Home zone position in the UI")
        return

    cfg = json.loads(cfg_path.read_text())
    data = cfg.get("data", {})
    try:
        cur_lat, cur_lon = float(data["latitude"]), float(data["longitude"])
        cur_radius = int(data.get("radius") or 100)
    except (KeyError, TypeError, ValueError):
        print("[i] Home zone has no usable position - set it in the UI")
        return

    veh_lat, veh_lon = vehicle
    gap = _haversine_m(cur_lat, cur_lon, veh_lat, veh_lon)
    if gap <= 500:
        print(f"[=] Home zone already near the car ({gap:.0f} m) - left unchanged")
        return

    shutil.copy(cfg_path, cfg_path.with_suffix(cfg_path.suffix + ".bak-zone"))
    data["latitude"] = round(veh_lat, 7)
    data["longitude"] = round(veh_lon, 7)
    data["radius"] = max(cur_radius, 100)
    cfg_path.write_text(json.dumps(cfg, indent=2))
    print(f"[+] Home zone was {gap:.0f} m from the car; re-anchored to "
          f"{data['latitude']}, {data['longitude']} (radius {data['radius']})")
    print("    (assumes the car is parked at home right now - adjust in the UI if not)")


def install_entry(config_dir: pathlib.Path, entry: dict,
                  vehicle: tuple[float, float] | None = None) -> int:
    storage = config_dir / ".storage"
    path = storage / "core.config_entries"
    if not storage.is_dir() or not path.is_file():
        print(f"[!] {config_dir} does not look like an HA config dir "
              f"(no .storage/core.config_entries)", file=sys.stderr)
        return 1

    backup = path.with_suffix(path.suffix + ".bak-configure")
    shutil.copy(path, backup)
    store = json.loads(path.read_text())

    for existing in store["data"]["entries"]:
        if existing.get("domain") == "xpeng" and existing.get("unique_id") == entry["unique_id"]:
            print(f"[!] an xpeng entry for VIN {entry['unique_id']} already exists "
                  f"(entry_id={existing['entry_id']}); refusing to duplicate")
            return 1
        if existing.get("domain") == "xpeng" and existing.get("entry_id") == entry["entry_id"]:
            entry["entry_id"] = secrets.token_hex(6).upper()

    store["data"]["entries"].append(entry)
    path.write_text(json.dumps(store, indent=2))
    print(f"[+] wrote entry {entry['entry_id']} (unique_id={entry['unique_id']}) into {path}")
    print(f"[+] backup: {backup}")

    # Do this in the same pass: a default Home zone is the classic "car shows away
    # while parked at home" report.
    ensure_home_zone(config_dir, vehicle)
    print()
    print("    Now restart Home Assistant:")
    print("      ha core restart          # or: docker restart homeassistant")
    print("    Then check: Settings -> Devices & Services -> XPENG (state: loaded)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default=None,
                    help="HA config dir (contains .storage/) to install the entry into; "
                         "omit to only print instructions")
    ap.add_argument("--ha-config-path", default="/config",
                    help="the config path AS SEEN BY THE INTEGRATION, used for the "
                         "token_file/tenant_file options (default /config)")
    ap.add_argument("--vin", default=None, help="override the VIN (default: read it from the account)")
    ap.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://127.0.0.1:8123"),
                    help="HA base URL, used only to read the car's position for the "
                         "Home-zone check (default $HA_URL or http://127.0.0.1:8123)")
    ap.add_argument("--ha-token", default=os.environ.get("HA_TOKEN", ""),
                    help="HA long-lived token for the Home-zone check (default $HA_TOKEN)")
    ap.add_argument("--vehicle-lat", type=float, default=None)
    ap.add_argument("--vehicle-lon", type=float, default=None)
    ap.add_argument("--tokens", default=str(TOKENS))
    ap.add_argument("--tenant", default=str(TENANT))
    args = ap.parse_args()

    if not pathlib.Path(args.tokens).is_file() or not pathlib.Path(args.tenant).is_file():
        print("[!] run tools/onboard_email_login.py --save first "
              "(need xpeng_tokens.json and xpeng_tenant.json)", file=sys.stderr)
        return 1

    api = load("xpeng_api", COMP / "xpeng_api.py")
    tokens = api.load_login_tokens(args.tokens)
    tenant = api.load_tenant_cache(args.tenant)

    if not tenant.get("deviceId"):
        print("[!] the tenant file has no deviceId: it is a pre-fix or mismatched "
              "tenant, and the integration would sign as the wrong device", file=sys.stderr)
        return 1

    vin = fetch_vin(api, tokens, tenant, args.vin)
    entry = build_entry(vin, tokens, tenant, args.ha_config_path)

    print(f"[*] tokens : uid={tokens.get('uid')}  ({args.tokens})")
    print(f"[*] tenant : deviceId={tenant['deviceId']}  teeId={str(tenant.get('teeId'))[:24]}")

    if args.config_dir:
        if args.vehicle_lat is not None and args.vehicle_lon is not None:
            vehicle = (args.vehicle_lat, args.vehicle_lon)
        else:
            entity = f"device_tracker.xpeng_{vin[-6:].lower()}_location"
            vehicle = last_vehicle_position(args.ha_url, args.ha_token, entity)
            if vehicle is None:
                print("[i] no car position available (set --ha-token / --vehicle-lat+lon, "
                      "or skip the Home-zone check)")
        return install_entry(pathlib.Path(args.config_dir), entry, vehicle)

    print_instructions(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
