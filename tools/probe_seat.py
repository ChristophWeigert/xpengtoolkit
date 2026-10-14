#!/usr/bin/env python3
"""Measure what a seat command actually does, on your own car.

Run it where Home Assistant is reachable, and give it a token. On the HA box the
defaults already work (`http://supervisor/core/api` plus `$SUPERVISOR_TOKEN`); from a
laptop set the two environment variables first:

    export HA_URL=http://homeassistant.local:8123/api
    read -s HA_TOKEN && export HA_TOKEN      # paste it; not echoed, not in history

    python3 tools/probe_seat.py                       # driver's seat, baseline only
    python3 tools/probe_seat.py --sequence            # driver: heat 1-3, ventilation 1-3
    python3 tools/probe_seat.py --seat rear-left --scan-cmd-types
    python3 tools/probe_seat.py --seat rear-left --cmd-type 3 --sequence

(`3` is the measured value for `rear-left`; `--scan-cmd-types` re-derives it on a car
whose rear seats may answer to something else. Nothing here may claim a rear value
before it is measured, which is why the scan is kept now that these two are known.)

Prints the car's own state fields around every command, so the mapping is measured
rather than assumed. That matters because the encodings are not what they look like:

  * `cmd_type` is **1 = driver, 0 = passenger, 3 = left rear, 4 = right rear**. An
    earlier version of the integration had the two front ones swapped, having read them
    off a right-hand-drive car where the driver's seat happens to be the right-hand one.
  * `cmd_value` is the **function**: 1 = heating, 2 = ventilation.
  * `mode` is the **level**: 0 = off, 1/2/3 = bar 1/2/3. It was long read as a momentary
    press — "the same payload sent twice toggles" — but those readings were taken ~2 s
    after each send, inside the car's 7-50 s apply/report window, so they showed the
    state from before the command. `--sequence` still exercises "off, then the level" for
    every level, which is the order measured to reproduce.

What this tool still exists to settle, and why it has the flags it has:

  * **Another car's rear `cmd_type`.** On the car these were measured on, `rear-left` is
    3 and `rear-right` is 4 (docs/SEAT_COMMANDS.md, 2026-10-14), each as a matched on/off
    pair on one value with the other rear seat not moving. Another car may answer to
    something else, so `--scan-cmd-types` tries every candidate and prints which state
    field each one moves. `5` is refused (`code 13023`), and `2` was accepted once and
    moved the *front passenger* field 46 s later.
  * **Level 3 on another car.** The app draws three bars plus off, and `mode 3` from off
    is a real level here: it reported 3 (2026-10-14). `--sequence` ends each function at
    level 3 anyway, so a car whose levels differ is caught rather than assumed.

The app is the oracle. Tapping the seat in the car picture cycles the bars, and this
prints the level the car reports beside them: the field that moved is the label, which
is what settles left/right against driver/passenger. A candidate payload that reaches
a state the app demonstrably reaches is a mapping; one that "succeeds" is not.

Three traps cost real time while working this out, and every one of them produces a
*wrong* reading rather than an error:

  * **The car sleeps and does not report.** A command may still be applied while it is
    asleep; it just will not be visible. This wakes it (`service_type 19`, `cmd_type 2`)
    after every command, not before.
  * **A seat sensor's `last_updated` does not mean freshness.** Home Assistant moves it
    only when the value *changes*, so a field resting at 0 keeps an old timestamp
    forever, however much data arrives. This reads the vehicle's own freshness signal
    separately: `..._last_data_timestamp`, the `timestamp`-class entity the component
    documents as the one to build on. (`..._last_data_update` also exists and carries
    the same coordinator field, rendered as a formatted string by the older sensor;
    either answers, and this one is machine-readable.)
  * **A flapping integration makes every reading meaningless.** If its MQTT session keeps
    being evicted — two sessions on one XPENG account, which is exactly what an app left
    open on the same account does — the entities cycle to `unavailable` and come back
    with *restored* values, not the car's. A state change read across that window
    attributes somebody else's action to your command. So no step is judged unless the
    car's own timestamp has advanced past the one read before it, and `unavailable` is
    treated as no data: `wait_for_fresh()` wakes and retries, and says so when it fails.
    Check `sensor.<vin>_polling_status` before believing a run - it should not read
    "Reconnecting".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

VIN_SLUG_FALLBACK = "123456"

#: Measured on the car. Do not change either without a measurement.
#:
#: The two rear values were measured on 2026-10-14 (docs/SEAT_COMMANDS.md): each has a
#: matched on/off pair on the same cmd_type, and the rear seat that was not addressed
#: did not move. `2` stays out of this map on purpose - it was accepted once and moved
#: the *front passenger* field 46 s later, which is not a mapping anything needs.
SEAT_CMD_TYPE = {"driver": 1, "passenger": 0, "rear-left": 3, "rear-right": 4}

#: The candidates worth trying when a car's rear values are not the measured ones.
#:
#: Kept whole - including 3 and 4 - because another car's rear seats may answer to
#: different values, and `--scan-cmd-types` is how that gets settled: on the car these
#: were measured on, 3 and 4 are the answer, and on another the scan is how it is
#: derived. It deliberately excludes the two measured front values: a candidate that
#: collides with a known value is not a candidate.
REAR_CMD_TYPE_CANDIDATES = (2, 3, 4, 5)

#: The app draws three bars plus off, and the car's own field is documented 0-3.
LEVELS = (1, 2, 3)

HEAT, VENTILATION, OFF = 1, 2, 0

#: Entity-id suffixes for each seat's two functions, after `sensor.xpeng_<vin-slug>_`.
#: The rear names are the car's own payload fields, not a guess about sides: the app's
#: own screen labels the rear seats left/right, so these are the fields to watch.
SEAT_FIELDS = {
    "driver": ("driver_seat_heating", "driver_seat_ventilation"),
    "passenger": ("passenger_seat_heating", "passenger_seat_ventilation"),
    "rear-left": ("left_rear_seat_heating", "left_rear_seat_ventilation"),
    "rear-right": ("right_rear_seat_heating", "right_rear_seat_ventilation"),
}

#: Every seat field, because a candidate `cmd_type` may move a seat nobody expected.
ALL_SEAT_FIELDS = tuple(field for pair in SEAT_FIELDS.values() for field in pair)

#: Freshness, as an entity-id suffix. The car reports nothing while it sleeps, so this
#: is the number that says whether a given reading is worth anything. `..._last_data_update`
#: is the older sensor showing the same field as a formatted string; this is the
#: `timestamp` entity the component points automations at.
FRESHNESS_FIELD = "last_data_timestamp"


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


def resolve_cmd_types(seat_name: str, cmd_type: int | None = None, scan: bool = False) -> list[int]:
    """Which `cmd_type` values to send, refusing to guess any that is unmeasured.

    A wrong value here moves a real seat. The rear seats are measured for the car this
    mapping came from (3 and 4), but another car may differ - so `--scan-cmd-types`
    still tries every candidate and reports which field each one moves, and an explicit
    `--cmd-type` still overrides both.
    """
    if scan:
        return list(REAR_CMD_TYPE_CANDIDATES)
    if cmd_type is not None:
        return [cmd_type]
    if seat_name in SEAT_CMD_TYPE:
        return [SEAT_CMD_TYPE[seat_name]]
    raise ValueError(
        "%s has no measured cmd_type: pass --cmd-type N, or --scan-cmd-types to find it"
        % seat_name
    )


def sequence_plan() -> list[tuple[int, int, str]]:
    """"Off, then press once" for every level of both functions.

    Level 3 is in here on purpose and is a real level: measured 2026-10-14, `mode 3`
    from off reported 3. The earlier "mode 3 turns the heating off" reading was taken
    ~2 s after the send, inside the car's 7-50 s apply/report window. The sequence still
    measures it rather than assuming it, because a car whose levels differ is exactly
    what this tool is for.
    """
    plan: list[tuple[int, int, str]] = []
    for function, function_name in ((HEAT, "heat"), (VENTILATION, "ventilation")):
        plan.append((OFF, function, "mode=0 (off)"))
        for level in LEVELS:
            plan.append((level, function, "mode=%d %s level %d" % (level, function_name, level)))
            plan.append((OFF, function, "mode=0 (off)"))
    return plan


def read(core: str, token: str, vin_slug: str, fields=None) -> dict[str, str]:
    out: dict[str, str] = {}
    for field in (fields if fields is not None else ALL_SEAT_FIELDS + (FRESHNESS_FIELD,)):
        status, body = _call(core, token, "GET", "/states/sensor.xpeng_%s_%s" % (vin_slug, field))
        try:
            out[field] = json.loads(body)["state"]
        except Exception:  # noqa: BLE001
            out[field] = "?" if status else "n/a"
    return out


def show(core: str, token: str, vin_slug: str, label: str,
         previous: dict[str, str] | None = None) -> dict[str, str]:
    """Print every seat field, and name the ones that moved since `previous`."""
    state = read(core, token, vin_slug)
    print("  %-26s heat d/p/rl/rr=%s/%s/%s/%s  vent d/p/rl/rr=%s/%s/%s/%s  last_data=%s" % (
        label,
        state["driver_seat_heating"], state["passenger_seat_heating"],
        state["left_rear_seat_heating"], state["right_rear_seat_heating"],
        state["driver_seat_ventilation"], state["passenger_seat_ventilation"],
        state["left_rear_seat_ventilation"], state["right_rear_seat_ventilation"],
        state[FRESHNESS_FIELD]))
    if previous is not None:
        moved = [
            "%s %s->%s" % (field, previous.get(field), state.get(field))
            for field in ALL_SEAT_FIELDS if state.get(field) != previous.get(field)
        ]
        print("      moved: %s" % (", ".join(moved) if moved else "nothing"))
    return state


#: Values that mean "this integration is not currently reporting", as opposed to a level.
UNKNOWN = ("unavailable", "unknown", "?", "n/a", "None", "")


def _epoch(state: dict[str, str]) -> float | None:
    """The car's own freshness timestamp as a number, or None if there isn't one."""
    raw = state.get(FRESHNESS_FIELD)
    if not raw or raw in UNKNOWN:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_fresh(state: dict[str, str], previous: dict[str, str] | None) -> bool:
    """Whether this reading is the car reporting *after* the command, not a stale echo.

    Two ways it is not: the car has not reported since the previous read (the timestamp
    did not advance), or the integration is mid-reconnect and the fields read
    `unavailable`. Either makes a "nothing moved" line prove nothing at all.
    """
    if any(state.get(field) in UNKNOWN for field in ALL_SEAT_FIELDS):
        return False
    now = _epoch(state)
    if now is None:
        return False
    before = _epoch(previous) if previous is not None else None
    return before is None or now > before


def wait_for_fresh(core: str, token: str, vin_slug: str, label: str, settle: int,
                   previous: dict[str, str] | None = None, attempts: int = 3
                   ) -> dict[str, str]:
    """Wake, wait, read - and refuse to treat a stale reading as a result."""
    state = previous
    for attempt in range(1, attempts + 1):
        wake(core, token)
        time.sleep(settle)
        state = show(core, token, vin_slug,
                     label if attempt == 1 else "%s (retry %d)" % (label, attempt),
                     previous)
        if is_fresh(state, previous):
            return state
        print("      [!] no fresh report - the car has not reported since the last read,"
              " or the integration is reconnecting. Waking again.")
    print("      [!] still no fresh report: this step proves nothing, whatever it shows.")
    return state


def step(core: str, token: str, vin_slug: str, cmd_type: int, mode: int, function: int,
         label: str, settle: int, previous: dict[str, str] | None = None) -> dict[str, str]:
    seat(core, token, cmd_type, mode, function)
    time.sleep(5)
    return wait_for_fresh(core, token, vin_slug, label, settle, previous)


def integration_is_usable(polling_status: str, credentials_valid: str) -> bool:
    """Whether the integration is actually connected to the car.

    A command sent into a dead session is not evidence, and it does not look like one:
    on 2026-10-14 the `.236` host answered "nothing moved" off a six-hour-old timestamp
    and showed a reconnect's restored zeros as seats turning off, with
    `polling_status` reading "Reconnecting" the whole time.
    """
    if "reconnect" in str(polling_status).lower():
        return False
    return str(credentials_valid).lower() not in ("off", "unavailable", "unknown")


def preflight(core: str, token: str, vin_slug: str, force: bool) -> bool:
    """Print the session's health, and refuse to measure through it when it is down."""
    readings = {}
    for entity, field in (
        ("sensor.xpeng_%s_polling_status" % vin_slug, "polling_status"),
        ("binary_sensor.xpeng_%s_credentials_valid" % vin_slug, "credentials_valid"),
    ):
        status, body = _call(core, token, "GET", "/states/" + entity)
        try:
            readings[field] = json.loads(body)["state"]
        except Exception:  # noqa: BLE001 - an older integration may not have the entity
            readings[field] = "n/a" if status else "n/a"
    print("integration: polling_status=%s credentials_valid=%s"
          % (readings["polling_status"], readings["credentials_valid"]))
    if not force and not integration_is_usable(readings["polling_status"],
                                               readings["credentials_valid"]):
        print("[-] the integration is not connected to the car, so every reading below")
        print("    would be a coin toss. Fix the session first, or pass --force.")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://supervisor/core/api"),
                        help="Home Assistant API root (default: the Supervisor proxy)")
    parser.add_argument("--token", default=os.environ.get("SUPERVISOR_TOKEN", "")
                        or os.environ.get("HA_TOKEN", ""),
                        help="bearer token (default: $SUPERVISOR_TOKEN or $HA_TOKEN)")
    parser.add_argument("--seat", choices=sorted(SEAT_FIELDS), default="driver",
                        help="which seat's fields to watch, and whose cmd_type to send")
    parser.add_argument("--cmd-type", type=int, default=None,
                        help="send this cmd_type instead of the measured one "
                             "(for a car whose rear seats differ from 3/4)")
    parser.add_argument("--scan-cmd-types", action="store_true",
                        help="try each rear candidate (%s) one at a time and print "
                             "which seat field each one moves"
                             % ",".join(str(c) for c in REAR_CMD_TYPE_CANDIDATES))
    parser.add_argument("--vin-slug", default=VIN_SLUG_FALLBACK,
                        help="the six digits of the VIN in the entity ids, e.g. 123456")
    parser.add_argument("--settle", type=int, default=35,
                        help="seconds to wait for the car to report after a wake")
    parser.add_argument("--sequence", action="store_true",
                        help="run off, then heat 1/2/3, off, then ventilation 1/2/3")
    parser.add_argument("--force", action="store_true",
                        help="probe even if the integration is not connected")
    args = parser.parse_args()

    if not args.token:
        print("[-] no token: pass --token, or set $SUPERVISOR_TOKEN / $HA_TOKEN", file=sys.stderr)
        return 2
    core = args.ha_url.rstrip("/")

    try:
        cmd_types = resolve_cmd_types(args.seat, args.cmd_type, args.scan_cmd_types)
    except ValueError as exc:
        print("[-] %s" % exc, file=sys.stderr)
        return 2

    status, _ = _call(core, args.token, "GET", "/config")
    if status != 200:
        print("[-] %s/config answered %s - wrong URL or token?" % (core, status), file=sys.stderr)
        return 2

    if not preflight(core, args.token, args.vin_slug, args.force):
        return 3

    print("%s seat, waking the car first so the baseline is real:" % args.seat)
    previous = wait_for_fresh(core, args.token, args.vin_slug, "baseline", args.settle)

    if args.scan_cmd_types:
        for cmd_type in cmd_types:
            if cmd_type != SEAT_CMD_TYPE.get(args.seat):
                print("\n[!] cmd_type=%d is not measured on this car - watch the car, not "
                      "this output" % cmd_type)
            print("\ncmd_type=%d:" % cmd_type)
            previous = step(core, args.token, args.vin_slug, cmd_type, OFF, HEAT,
                            "mode=0 (off)", args.settle, previous)
            previous = step(core, args.token, args.vin_slug, cmd_type, 1, HEAT,
                            "mode=1 heat 1", args.settle, previous)
        print("\nthe candidate whose 'mode=1 heat 1' moved the target seat's heat field")
        print("from 0 to 1 is the answer. Confirm it with:")
        print("  --seat %s --cmd-type <N> --sequence" % args.seat)
        return 0

    if args.sequence:
        plan = sequence_plan()
        for cmd_type in cmd_types:
            for mode, function, label in plan:
                previous = step(core, args.token, args.vin_slug, cmd_type, mode, function,
                                label, args.settle, previous)
        print("\nread the level the car reports back, not the command that was sent:")
        print("  heat level N should follow 'off then mode=N' for the heating function,")
        print("  and the same for ventilation - they are separate fields. A level that")
        print("  never appears is not reachable through this encoding, whatever the app"
              " draws.")
        return 0

    print("\nnothing sent. Use --sequence for the standard levels, --scan-cmd-types to")
    print("find a rear seat's cmd_type, or import this module to drive one at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
