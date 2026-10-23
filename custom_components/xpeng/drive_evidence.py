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
        "coord_valid": coord_valid(state),
    }


#: ``interval.drive.coordValid`` encodings. The car sets 1 when the coordinates
#: in this frame are a current fix and 0 when it is reporting its last known
#: position instead. Anything we cannot read is None - "the car did not say" -
#: and is treated as valid, so a car that never sends the flag keeps working.
_GPS_VALID_TRUE: tuple[str, ...] = ("1", "true", "yes", "valid")
_GPS_VALID_FALSE: tuple[str, ...] = ("0", "false", "no", "invalid", "")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _GPS_VALID_TRUE:
            return True
        if text in _GPS_VALID_FALSE:
            return False
    return None


def coord_valid(state: Any) -> bool | None:
    """Whether the coordinates in *state* are a current fix, or None if unsaid.

    Reported from a G9 owner (2026-10-23): Home Assistant still showed the car at
    home after a drive, and a wake was needed before the new position appeared.
    The car's telemetry carries this flag for exactly that case - a frame whose
    coordinates are the last known position rather than a new one - and the
    integration was publishing whatever coordinates arrived, unconditionally.
    """
    content = state.get("msg_content") if isinstance(state, dict) else None
    return _as_bool(_dig(content, ("interval", "drive", "coordValid")))


def position(state: Any) -> tuple[float, float] | None:
    """The usable ``(latitude, longitude)`` in *state*, or None.

    ``0`` is not a position (the car sends it when it has none), and half a
    position is not one either - which is the same rule the device tracker has
    always applied.
    """
    content = state.get("msg_content") if isinstance(state, dict) else None
    drive = _dig(content, ("interval", "drive"))
    if not isinstance(drive, dict):
        return None
    latitude = _as_float(drive.get("latitude"))
    longitude = _as_float(drive.get("longitude"))
    if not latitude or not longitude:
        return None
    return (latitude, longitude)


def with_previous_fix(state: Any, previous_state: Any = None) -> Any:
    """Keep an unusable fix from being published as the car's position.

    When a frame says its coordinates are not a current fix (``coordValid`` is
    false) or carries no usable coordinates at all, the last valid position is
    carried forward instead - and with nothing to carry, the coordinates are
    dropped rather than published. Other fields in the frame are untouched, so
    a stale fix cannot take the rest of the telemetry with it.

    The same object comes back when there is nothing to repair, so callers can
    skip rebuilding the state by identity alone.
    """
    content = state.get("msg_content") if isinstance(state, dict) else None
    drive = _dig(content, ("interval", "drive"))
    if not isinstance(drive, dict):
        return state
    carries_coords = "latitude" in drive or "longitude" in drive
    if coord_valid(state) is not False and (
        position(state) is not None or not carries_coords
    ):
        return state
    previous = position(previous_state)
    repaired = dict(drive)
    if previous is None:
        repaired.pop("latitude", None)
        repaired.pop("longitude", None)
    else:
        repaired["latitude"], repaired["longitude"] = previous
    interval = content.get("interval")
    interval = dict(interval) if isinstance(interval, dict) else {}
    interval["drive"] = repaired
    return {**state, "msg_content": {**content, "interval": interval}}


class FixTracker:
    """The last position that was actually a fix, and how old it is.

    Fed every accepted state *after* :func:`with_previous_fix`, so an invalid
    frame reports the carried-forward position and does not look fresher than
    it is. ``fixed_at`` only moves when a frame marks the fix valid (or says
    nothing about validity and carries usable coordinates).
    """

    def __init__(self) -> None:
        self.coord_valid: bool | None = None
        self.latitude: float | None = None
        self.longitude: float | None = None
        self.fixed_at: float | None = None

    @property
    def position(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (self.latitude, self.longitude)

    def observe(self, state: Any, now: float | None = None) -> None:
        """Record one accepted state's validity and, if usable, its position.

        ``now`` is where the fix came from in wall-clock seconds; None records
        the position without claiming a timestamp (used when restoring a cached
        state whose age is unknown).
        """
        self.coord_valid = coord_valid(state)
        found = position(state)
        if found is None:
            return
        self.latitude, self.longitude = found
        if self.coord_valid is not False and now is not None:
            self.fixed_at = now

    def age_seconds(self, now: float) -> float | None:
        if self.fixed_at is None:
            return None
        return max(0.0, now - self.fixed_at)
