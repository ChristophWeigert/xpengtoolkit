"""What one vehicle-state payload says about driving, and where it says it.

Born from a user report (issue #3, capture of 2026-10-21): during a drive the
integration spent 45 minutes sending passive queries and receiving 309-byte
stubs, while the dashboard's speed sensor and the GPS tracker read
``interval.drive.*`` and the parked/moving classifier read ``instant.drive.*``.
Upstream logged neither, so the capture could not say which field had misled
the classifier - which is the gap this module starts closing.

Two jobs, both pure so a test can pin them without a car:

* :func:`view` reads one payload into the handful of fields the classifier and
  the diagnostics need - instant speed and shift_state, interval speed, GPS
  presence - and returns the parked verdict together with the reason it reached
  it. The rules are exactly the ones ``XPengDataCoordinator.vehicle_parked``
  has always used; they live here now so they are testable and so the verdict
  and its explanation cannot drift apart.
* ``moving`` in that view answers a different question from the verdict: "was
  the car moving the last time it reported?". ``parked is None`` means *no
  evidence*, not parking - and ``interval`` speed is the field the car actually
  updates while it is being driven, which is why a missing or stale
  ``instant.drive`` block no longer has to be read as "parked".

``moving`` is what the watchdog uses to decide that a force-upload cannot wake
the car (see ``watchdog_policy.should_escalate_moving``), so it errs towards
"No" on anything it cannot read.
"""

from __future__ import annotations

from typing import Any

#: Shift encodings seen on this platform: strings from the MQTT push and small
#: ints from the REST widget. 3 and 4 both mean P on the measurements we have.
PARK_SHIFT_CODES: tuple[int, ...] = (3, 4)


def _dig(container: Any, keys: tuple[str, ...]) -> Any:
    node = container
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


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


def _shift_park(shift: Any) -> bool | None:
    """Whether a shift_state means parked; None when it cannot be read."""
    if isinstance(shift, str):
        return shift.strip().upper() == "P"
    if isinstance(shift, bool) or shift is None:
        return None
    try:
        return int(shift) in PARK_SHIFT_CODES
    except (TypeError, ValueError):
        return None


def view(state: Any) -> dict[str, Any]:
    """Read a vehicle state into the driving fields and the parked verdict.

    Returns a dict with the raw values (``shift_state``, ``instant_speed``,
    ``interval_speed``), ``gps`` as a ``(latitude, longitude)`` pair or None,
    the ``parked`` verdict (True / False / None), the ``reason`` behind it, and
    ``moving`` - the evidence the escalation policy acts on.
    """
    content = state.get("msg_content") if isinstance(state, dict) else None
    instant = content.get("instant") if isinstance(content, dict) else None
    drive = instant.get("drive") if isinstance(instant, dict) else None
    interval_drive = _dig(content, ("interval", "drive"))

    shift_state = drive.get("shift_state") if isinstance(drive, dict) else None
    instant_speed = drive.get("speed") if isinstance(drive, dict) else None
    interval_speed = (
        interval_drive.get("speed") if isinstance(interval_drive, dict) else None
    )
    latitude = (
        interval_drive.get("latitude") if isinstance(interval_drive, dict) else None
    )
    longitude = (
        interval_drive.get("longitude") if isinstance(interval_drive, dict) else None
    )
    gps = (
        (latitude, longitude)
        if latitude is not None and longitude is not None
        else None
    )

    parked: bool | None
    if not drive:
        parked, reason = None, "no instant.drive block"
    else:
        speed = _as_float(instant_speed)
        if speed is not None and speed > 0:
            parked, reason = False, f"instant speed {speed:g} km/h"
        elif shift_state is None:
            parked, reason = None, "no shift_state"
        else:
            shift_park = _shift_park(shift_state)
            if shift_park is None:
                parked, reason = None, f"unreadable shift_state {shift_state!r}"
            elif shift_park:
                parked, reason = True, f"shift_state {shift_state!r}"
            else:
                parked, reason = False, f"shift_state {shift_state!r}"

    interval_speed_kmh = _as_float(interval_speed)
    moving = parked is False or (
        interval_speed_kmh is not None and interval_speed_kmh > 0
    )
    return {
        "parked": parked,
        "reason": reason,
        "moving": moving,
        "shift_state": shift_state,
        "instant_speed": instant_speed,
        "interval_speed": interval_speed,
        "gps": gps,
    }
