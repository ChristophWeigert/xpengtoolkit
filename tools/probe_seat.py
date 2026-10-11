#!/usr/bin/env python3
"""Measure what a seat command actually does, on your own car.

    python3 tools/probe_seat.py                     # driver's seat, standard sequence
    python3 tools/probe_seat.py --seat passenger
    python3 tools/probe_seat.py --ha-url http://homeassistant.local:8123/api --token <long-lived>

Prints the car's own state fields around every command, so the mapping is measured
rather than assumed. That matters because the encodings are not what they look like:

  * `cmd_type` is **1 = driver, 0 = passenger**. An earlier version of the integration
    had these swapped, having read them off a right-hand-drive car where the driver's
    seat happens to be the right-hand one.
  * `cmd_value` is the **function**: 1 = heating, 2 = ventilation.
  * `mode` is a **press** at that level, not a level to set. Sending the same payload
    twice toggles, and pressing a level while another is on can switch the function
    *off*. `mode 0` is off. The reliable order is "off, then press the target once",
    which is what `--sequence` exercises.

Two traps cost real time while working this out, and both produce a *wrong* reading
rather than an error:

  * **The car sleeps and does not report.** A command may still be applied while it is
    asleep; it just will not be visible. This wakes it (`service_type 19`, `cmd_type 2`)
    after every command, not before.
  * **A seat sensor's `last_updated` does not mean freshness.** Home Assistant moves it
    only when the value *changes*, so a field resting at 0 keeps an old timestamp
    forever. `sensor.<vehicle>_last_data_update` is the signal that the car reported;
    this prints it next to the seat fields for that reason.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

VIN_SLUG_FALLBACK = "800783"
SEAT_CMD_TYPE = {"driver": 1, "passenger": 0}
HEAT, VENTILATION, OFF = 1, 2, 0


def _call(core: str, token: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        core + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        return 0, "%s: %s" % (type(exc).__name__, exc)


def _send(core: str, token: str, service_type: int, msg_content: dict, msg_type: int = 2):
    return _call(core, token, "POST", "/services/xpeng/send_raw_mqtt",
                 {"service_type": service_type, "msg_type": msg_type,
                  "msg_content": json.dumps(msg_content)})


def wake(core: str, token: str) -> None:
    """Ask the car for a state upload. Without this a sleeping car looks unchanged."""
    _send(core, token, 19, {"cmd_type": 2})


def seat(core: str, token: str, cmd_type: int, mode: int, function: int) -> None:
    _send(core, token, 30, {"cmd_type": cmd_type, "mode": mode, "cmd_value": function})


def read(core: str, token: str, vin_slug: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for field in ("driver_seat_heating", "driver_seat_ventilation",
                  "passenger_seat_heating", "passenger_seat_ventilation",
                  "last_data_update"):
        status, body = _call(core, token, "GET", "/states/sensor.xpeng_%s_%s" % (vin_slug, field))
        try:
            out[field] = json.loads(body)["state"]
        except Exception:  # noqa: BLE001
            out[field] = "?" if status else "n/a"
    return out


def show(core: str, token: str, vin_slug: str, label: str) -> dict[str, str]:
    state = read(core, token, vin_slug)
    print("  %-30s heat(d/p)=%s/%s vent(d/p)=%s/%s  last_data=%s" % (
        label, state["driver_seat_heating"], state["passenger_seat_heating"],
        state["driver_seat_ventilation"], state["passenger_seat_ventilation"],
        state["last_data_update"]))
    return state


def step(core: str, token: str, vin_slug: str, cmd_type: int, mode: int, function: int,
         label: str, settle: int) -> None:
    seat(core, token, cmd_type, mode, function)
    time.sleep(5)
    wake(core, token)          # the command may have landed while the car was asleep
    time.sleep(settle)
    show(core, token, vin_slug, label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://supervisor/core/api"),
                        help="Home Assistant API root (default: the Supervisor proxy)")
    parser.add_argument("--token", default=os.environ.get("SUPERVISOR_TOKEN", "")
                        or os.environ.get("HA_TOKEN", ""),
                        help="bearer token (default: $SUPERVISOR_TOKEN or $HA_TOKEN)")
    parser.add_argument("--seat", choices=sorted(SEAT_CMD_TYPE), default="driver")
    parser.add_argument("--vin-slug", default=VIN_SLUG_FALLBACK,
                        help="the six digits of the VIN in the entity ids, e.g. 800783")
    parser.add_argument("--settle", type=int, default=35,
                        help="seconds to wait for the car to report after a wake")
    parser.add_argument("--sequence", action="store_true",
                        help="run heat 1, heat 2, off, ventilation 1, ventilation 2, off")
    args = parser.parse_args()

    if not args.token:
        print("[-] no token: pass --token, or set $SUPERVISOR_TOKEN / $HA_TOKEN", file=sys.stderr)
        return 2
    core = args.ha_url.rstrip("/")
    cmd_type = SEAT_CMD_TYPE[args.seat]

    status, _ = _call(core, args.token, "GET", "/config")
    if status != 200:
        print("[-] %s/config answered %s - wrong URL or token?" % (core, status), file=sys.stderr)
        return 2

    print("%s seat (cmd_type=%d), waking the car first so the baseline is real:" % (
        args.seat, cmd_type))
    wake(core, args.token)
    time.sleep(args.settle)
    show(core, args.token, args.vin_slug, "baseline")

    if args.sequence:
        for mode, function, label in (
            (OFF, HEAT, "mode=0 (off)"),
            (1, HEAT, "mode=1 heat level 1"),
            (OFF, HEAT, "mode=0 (off)"),
            (2, HEAT, "mode=2 heat level 2"),
            (OFF, HEAT, "mode=0 (off)"),
            (1, VENTILATION, "mode=1 ventilation 1"),
            (OFF, VENTILATION, "mode=0 (off)"),
            (2, VENTILATION, "mode=2 ventilation 2"),
            (OFF, VENTILATION, "mode=0 (off)"),
        ):
            step(core, args.token, args.vin_slug, cmd_type, mode, function, label, args.settle)
        print("\nread the level the car reports back, not the command that was sent:")
        print("  heat level N should follow 'off then mode=N' for the heating function,")
        print("  and the same for ventilation - they are separate fields.")
        return 0

    print("\nnothing sent. Use --sequence for the standard nine commands, or import")
    print("this module to drive one at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
