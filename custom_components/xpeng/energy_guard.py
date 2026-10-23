"""An older snapshot is not energy data - and a parked car's SoC cannot rise.

The battery sensor flapped between two values for hours, on two different cars,
for two different reasons that share one fact: state of charge only falls while
a car is parked and idle, so a reading that rises is a snapshot from before the
drain rather than a new measurement.

Measured on the reference G6, 2026-10-23 17:49-20:04 (Australia/Sydney). The
last push carrying charge/odometer arrived at 17:49:42 with SOC 50. The push
fan-out then wedged; the car answered queries with acks. The REST widget
fallback - the live value, polled every ``parked_scan_interval`` - wrote 49 at
19:27:10, 19:42:56 and 19:58:41 ("Merged widget_state ... MQTT silent for
515s/561s/606s", and the recorder's 19:27:10.220718 row matches the log line to
the millisecond). Between those merges, charge-less partial frames made the
MQTT client push its own baseline - still the frozen 50 - through
``set_vehicle_data``, so the sensor went 49 -> 50 -> 49 -> 50 every ~15 min for
an hour. ``replay_guard.is_replay`` could not see it: the odometer is frozen
while the car is parked, and the guard needs it to fall.

The G9 report of the same day is the same fight with a longer stall: a high
value frozen on the history graph for hours, notched down to a slowly declining
one. There the replay arrives as a whole snapshot rather than a baseline
re-broadcast, and the odometer is equally uninformative.

So this module adds the one signal the energy itself carries - direction - and
the acceptance path rejects a state whose state of charge rises while the car
is parked and not charging. Rejecting it rather than carrying the held value
forward matters: a refused state is kept out of the "a message carried data"
clock, which is what lets the silence watchdog escalate to the force-upload
that heals a wedged fan-out (the same reason ``replay_guard.py`` refuses a
falling odometer rather than repairing it).

The tolerance is ``SOC_ROUNDING_TOLERANCE_PCT``: the resolution of the value,
and the size of the BMS wobble seen while sitting, so a reading that merely
rounds differently is not called a replay. While charging, and whenever the
parked verdict is not known, no verdict is returned - charging is the one
legitimate reason for a parked car's SoC to rise.
"""

from __future__ import annotations

from typing import Any

#: The resolution of ``battery_soc``. A rise within this many percent is inside
#: the noise (rounding, a BMS recalibration while sitting), not a replay.
SOC_ROUNDING_TOLERANCE_PCT = 1.0


def _as_float(value: Any) -> float | None:
    """A number, or None when the value is not one (bools are not numbers)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _dig(container: Any, *keys: str) -> Any:
    node = container
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def soc_of(state: Any) -> float | None:
    """The state of charge in a coordinator state (``msg_content.instant...``)."""
    return _as_float(_dig(state, "msg_content", "instant", "charge", "battery_soc"))


def state_is_charging(state: Any) -> bool:
    """Whether a state says a charge session is running.

    The same two signals the coordinator's ``is_charging`` uses: the car's own
    ``charging_state`` 2, and non-zero ``interval.charge.power``, which catches
    a charger the car has not admitted to yet (EVCC and friends).
    """
    if not isinstance(state, dict):
        return False
    content = state.get("msg_content")
    if not isinstance(content, dict):
        return False
    instant = content.get("instant")
    if isinstance(instant, dict) and _dig(instant, "charge", "charging_state") == 2:
        return True
    interval = content.get("interval")
    if isinstance(interval, dict):
        power = _as_float(_dig(interval, "charge", "power"))
        if power is not None and power > 0:
            return True
    return False


def parked_soc_replay(
    candidate: Any,
    held: Any,
    *,
    parked: bool | None,
    charging: bool,
    tolerance: float = SOC_ROUNDING_TOLERANCE_PCT,
) -> bool:
    """Whether *candidate* is an older snapshot of a parked, idle car.

    No verdict unless the caller has already established that the car was
    parked and not charging - the conditions under which state of charge can
    only fall. ``charging`` should account for the candidate as well as the
    held state, so a frame that starts a charge session is never refused.
    """
    if parked is not True or charging:
        return False
    held_soc = soc_of(held)
    candidate_soc = soc_of(candidate)
    if held_soc is None or candidate_soc is None:
        return False
    return candidate_soc > held_soc + tolerance
