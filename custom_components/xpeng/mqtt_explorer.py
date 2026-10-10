#!/usr/bin/env python3
"""Quick MQTT topic explorer for XPENG broker.

Subscribes to wildcard topics and prints everything received.
Uses the API to get fresh credentials automatically.

Usage:
    python3 mqtt_explorer.py [--wildcard 'Xmart/#'] [--duration 60]
    python3 mqtt_explorer.py --cert-dir /config/xpeng-certs

The broker certificate is verified against ca-chain.crt (no insecure mode);
--cert-dir defaults to ./certs beside this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from xpeng_api import (
    DEFAULT_APP_CONFIG,
    create_mqtt_account,
    fetch_tenant,
    load_login_tokens,
    load_tenant_cache,
    mint_device_id,
    save_tenant_cache,
)

import paho.mqtt.client as mqtt

# Sibling module in this directory; run as a script so a plain (non-relative)
# import is correct. Reuses the integration's verifying TLS context builder.
from mqtt_client import build_tls_context

CERT_DIR = Path(SCRIPT_DIR) / "certs"
SEEN_TOPICS: dict[str, int] = {}


def resolve_device_id(
    explicit: str, tenant: dict | None, tenant_path: str
) -> tuple[str, str]:
    """The device id to sign as, and where it came from.

    The shipped default no longer pins one, so the REST layer raises rather than
    impersonate the author's device. A tenant is bound to the id that fetched it
    — its key pool is sealed with md5(deviceId) as the AES key — so an id the
    tenant already records must be adopted; replacing it makes every call answer
    12010104 "Login status expired". With nothing to adopt this is a first run,
    so mint one.
    """
    if explicit and explicit.strip():
        return explicit.strip(), "--device-id"
    stored = str((tenant or {}).get("deviceId") or "").strip()
    if stored:
        return stored, f"adopted from {tenant_path}"
    return mint_device_id(), "minted (no device id in the tenant)"


def on_connect(client, userdata, flags, rc, properties=None):
    wildcards = userdata.get("wildcards", ["Xmart/#"])
    if rc == 0:
        print(f"\n[CONNECTED] rc={rc}")
        for wc in wildcards:
            client.subscribe(wc, qos=0)
            print(f"  Subscribed: {wc}")
        print(f"\nListening... (Ctrl+C to stop)\n{'='*70}")
    else:
        print(f"[CONNECT FAILED] rc={rc}")


def on_message(client, userdata, msg):
    topic = msg.topic
    SEEN_TOPICS[topic] = SEEN_TOPICS.get(topic, 0) + 1
    payload_len = len(msg.payload)

    # Try decode as JSON
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        payload_str = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        # Try plain text
        try:
            payload_str = msg.payload.decode("utf-8")
        except Exception:
            payload_str = f"<binary {payload_len} bytes: {msg.payload[:50].hex()}...>"

    ts = time.strftime("%H:%M:%S")
    print(f"\n[{ts}] Topic: {topic}  ({payload_len} bytes, seen {SEEN_TOPICS[topic]}x)")
    # Truncate very long payloads
    if len(payload_str) > 2000:
        print(f"  {payload_str[:2000]}...")
        print(f"  ... ({len(payload_str)} chars total)")
    else:
        print(f"  {payload_str}")


def main():
    parser = argparse.ArgumentParser(description="XPENG MQTT topic explorer")
    parser.add_argument(
        "--wildcard", "-w", action="append", default=None,
        help="MQTT wildcard to subscribe (default: Xmart/#). Can specify multiple.",
    )
    parser.add_argument(
        "--duration", "-d", type=int, default=120,
        help="How long to listen in seconds (default: 120)",
    )
    parser.add_argument(
        "--token-file", default="xpeng_tokens.json",
        help="Token file path",
    )
    parser.add_argument(
        "--tenant-file", default="xpeng_tenant.json",
        help="Tenant cache file path",
    )
    parser.add_argument(
        "--cert-dir", default=None,
        help=(
            "Directory holding ca-chain.crt, client-cert-0.pem and "
            f"client-key.pem (default: {CERT_DIR})"
        ),
    )
    parser.add_argument(
        "--device-id", default=None,
        help=(
            "32-hex device id to sign as. Default: the id the tenant on disk "
            "was minted under, else a freshly minted one — never a pinned "
            "device this install does not own."
        ),
    )
    parser.add_argument(
        "--send-query", action="store_true", default=True,
        help="Send a state query after connecting (default: True)",
    )
    args = parser.parse_args()

    # Known XPENG broker topics from routing config
    DEFAULT_WILDCARDS = [
        "Xmart/#",                              # catch-all for Xmart
        "Xmart/Car/+/State/CanPack1s",          # d20 CAN bus 1s (car VID)
        "Xmart/Tbox/+/CanPack1s",               # e28 CAN bus 1s (T-box)
        "Xmart/TboxD21B/+/CanPack1s",           # d21b CAN bus 1s (T-box)
        "Xmart/Tbox/+/StatPack",                # T-box status
        "Xmart/Client/Connect/+",               # Client connect events
        "Xmart/Log/statPack/Repub/+",           # Log stats (CDU)
        "Xmart/Car/+/CanPackV2/Repub",          # CAN pack v2 (car VID)
        # $local/$queue variants (EMQX shared subscription)
        "$local/$queue/Xmart/Car/+/State/CanPack1s",
        "$local/$queue/Xmart/Tbox/+/CanPack1s",
        "$local/$queue/Xmart/TboxD21B/+/CanPack1s",
        "$local/$queue/Xmart/Tbox/+/StatPack",
        "$local/$queue/Xmart/Send/Emq/Dial",
    ]
    wildcards = args.wildcard or DEFAULT_WILDCARDS

    # Resolve paths relative to script dir
    token_path = args.token_file
    if not os.path.isabs(token_path):
        token_path = os.path.join(SCRIPT_DIR, token_path)
    tenant_path = args.tenant_file
    if not os.path.isabs(tenant_path):
        tenant_path = os.path.join(SCRIPT_DIR, tenant_path)

    # Get fresh MQTT credentials via API
    print("Getting fresh MQTT credentials...")
    config = dict(DEFAULT_APP_CONFIG)
    tokens = load_login_tokens(token_path)

    try:
        tenant = load_tenant_cache(tenant_path)
    except Exception:
        tenant = None

    # Sign as the id the tenant was minted under (or, with no tenant at all,
    # as a fresh one). This has to happen before fetch_tenant(): that call is
    # itself signed, and the id it uses is the id the new tenant gets sealed
    # against.
    device_id, device_id_origin = resolve_device_id(
        args.device_id, tenant, tenant_path
    )
    config["ConfiguredDeviceId"] = device_id
    print(f"  deviceId:  {device_id}  ({device_id_origin})")

    if tenant is None:
        print("  Fetching tenant...")
        tenant = fetch_tenant(config)
        save_tenant_cache(tenant_path, tenant)

    account = create_mqtt_account(config, tenant, tokens)
    client_id = account["clientId"]
    password = account["password"]
    server_ssl = account.get("serverSsl", "iot-app-global.xiaopeng.com:8885")
    expires_in = account.get("expireInSec", "?")

    # Parse user_id from client_id
    parts = client_id.split(":")
    user_id = parts[2] if len(parts) >= 3 else "unknown"

    print(f"  clientId:  {client_id}")
    print(f"  userId:    {user_id}")
    print(f"  server:    {server_ssl}")
    print(f"  expiresIn: {expires_in}s")

    # Parse server
    host_port = server_ssl.split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 8885

    # Create MQTT client
    client = mqtt.Client(
        client_id=client_id,
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        clean_session=True,
        userdata={"wildcards": wildcards, "user_id": user_id},
    )
    client.username_pw_set(client_id, password)
    client.on_connect = on_connect
    client.on_message = on_message

    # mTLS — same verifying context as the integration: the XPENG CA chain is
    # loaded and both the broker certificate and the hostname are required.
    # (This used to call tls_insecure_set(True), which skipped broker
    # verification entirely even though the CA had been loaded.)
    cert_dir = Path(args.cert_dir) if args.cert_dir else CERT_DIR
    try:
        client.tls_set_context(build_tls_context(cert_dir))
    except FileNotFoundError as err:
        print(f"ERROR: {err}")
        return 1

    print(f"\nConnecting to {host}:{port}...")
    client.connect(host, port, keepalive=60)
    client.loop_start()

    # Wait for connection
    time.sleep(2)

    # Send state query if requested
    if args.send_query:
        send_topic = f"Xmart/Send/User/{user_id}"
        query_msg = {
            "msg_type": 4,
            "service_type": 19,
            "msg_content": {},
        }
        client.publish(send_topic, json.dumps(query_msg), qos=0)
        print(f"\n[SENT] State query on {send_topic}")

        # Also send force upload
        wake_msg = {
            "msg_type": 2,
            "service_type": 19,
            "msg_content": {"cmd_type": 2},
        }
        client.publish(send_topic, json.dumps(wake_msg), qos=0)
        print(f"[SENT] Force upload on {send_topic}")

    try:
        end_time = time.time() + args.duration
        while time.time() < end_time:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    print(f"\n\n{'='*70}")
    print("Summary of topics seen:")
    for topic, count in sorted(SEEN_TOPICS.items()):
        print(f"  {topic}: {count} messages")
    if not SEEN_TOPICS:
        print("  (no messages received)")

    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
