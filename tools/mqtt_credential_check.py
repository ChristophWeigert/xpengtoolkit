#!/usr/bin/env python3
"""Verify the saved login tokens can mint a working MQTT connection.

Reads custom_components/xpeng/{xpeng_tokens.json,xpeng_tenant.json}, calls
createMqttAccount, then does a real mTLS connect to the returned broker and
subscribes to Xmart/Msg/User/<uid>. Prints the broker's CONNACK result.
"""
from __future__ import annotations
import importlib.util, json, ssl, sys, time
from pathlib import Path

import paho.mqtt.client as mqtt

COMP = Path(__file__).resolve().parent.parent / "custom_components" / "xpeng"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def main() -> int:
    api = load("xpeng_api", COMP / "xpeng_api.py")
    cfg = dict(api.DEFAULT_APP_CONFIG)
    tokens = api.load_login_tokens(str(COMP / "xpeng_tokens.json"))
    tenant = api.load_tenant_cache(str(COMP / "xpeng_tenant.json"))

    acct = api.create_mqtt_account(cfg, tenant, tokens)
    print("clientId :", acct["clientId"])
    print("server   :", acct["serverSsl"], "| expires in", acct.get("expireInSec"), "s")

    host, port = acct["serverSsl"].split(":")
    certs = COMP / "certs"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(str(certs / "client-cert-0.pem"), str(certs / "client-key.pem"))
    ctx.load_verify_locations(cafile=str(certs / "ca-chain.crt"))

    result = {"rc": None, "msgs": []}
    c = mqtt.Client(client_id=acct["clientId"], protocol=mqtt.MQTTv311,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    clean_session=True)
    c.username_pw_set(acct["clientId"], acct["password"])
    c.tls_set_context(ctx)

    def on_connect(_c, _u, _f, rc, _p=None):
        result["rc"] = rc
        print("CONNACK  :", rc)
        if str(rc) in ("0", "Success"):
            _c.subscribe(f"Xmart/Msg/User/{tokens['uid']}", qos=0)

    def on_message(_c, _u, msg):
        result["msgs"].append((msg.topic, len(msg.payload)))

    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(host, int(port), keepalive=60)
    c.loop_start()
    for _ in range(150):
        if result["rc"] is not None:
            break
        time.sleep(0.1)
    time.sleep(2)
    c.loop_stop(); c.disconnect()
    ok = str(result["rc"]) in ("0", "Success")
    print("RESULT   :", "PASS" if ok else "FAIL",
          f"| messages seen: {len(result['msgs'])}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
