"""What the integration is doing right now, and when it will next ask.

The integration polls XPENG on three independent schedules — a periodic MQTT
query, an hourly-cost REST poll, and a silence watchdog that reconnects when the
cloud stops delivering. None of that was visible anywhere: the only diagnostic
that existed, `last_data_update`, says when data last arrived, which is the one
question ("is it working?") it cannot answer. A parked car and a wedged cloud
fanout both look like an old timestamp.

This module holds the parts of the answer that are arithmetic or vocabulary, with
no Home Assistant import, so they can be tested. The rest is in
`custom_components/xpeng/sensor.py` and `__init__.py`, which observe live state.

Two things are deliberately separate here:

  * **the cadence** — which of the two intervals the periodic timer is using,
    decided exactly as `_send_periodic_query` decides it (`is_charging`, or
    `vehicle_parked is False`). If this ever disagrees with the timer, the sensor
    is lying about the thing it exists to report, so it takes the same inputs and
    is pinned by test against the same rule.
  * **the health** — whether the connection is up, and whether anything has
    actually arrived.
"""

from __future__ import annotations

#: Charging or driving: force-upload every `scan_interval`, because the car is
#: already awake and the autonomous push stream cannot be relied on.
ACTIVE = "Active"

#: Parked and quiet: one passive query per `parked_scan_interval`, which a
#: sleeping car answers with a stub. Normal, and not a fault.
PARKED = "Parked"

#: Connected, but nothing has arrived for longer than the silence threshold —
#: the state the watchdog reconnects for.
SILENT = "Silent"

#: As SILENT, and a forced upload has already been sent to try to punch through.
ESCALATED = "Escalated"

#: The socket itself is down.
RECONNECTING = "Reconnecting"


def is_active(parked: bool | None, is_charging: bool) -> bool:
    """Whether the car is awake and being driven or charged.

    The same expression as `_send_periodic_query`: `vehicle_parked` is a
    tri-state, and `None` (no data yet) must count as *not* active, because
    nothing has told us the car is moving.
    """
    return bool(is_charging) or parked is False


def query_interval_seconds(
    parked: bool | None,
    is_charging: bool,
    scan_interval: float,
    parked_scan_interval: float,
) -> float:
    """How often the periodic MQTT query is allowed to fire right now."""
    return scan_interval if is_active(parked, is_charging) else parked_scan_interval


def next_query_at(last_sent: float, now: float, interval: float) -> float:
    """When the periodic query next becomes *due*.

    Not necessarily the moment it is sent: the timer that acts on it fires on
    its own fixed cadence, so a query that falls due between two ticks waits for
    the next one. That granularity is reported alongside rather than folded in,
    because pretending to a precision the timer does not have would make the
    number wrong in a way nobody could see.

    Clamped to `now`, so an overdue query reads as "due now" rather than as a
    timestamp in the past, which is how a `device_class: timestamp` sensor would
    otherwise render it — as something that should already have happened.
    """
    return max(now, last_sent + interval)


def escalation_pending(stale_seconds: float, last_escalation: float,
                       now: float, *, escalate_after: float,
                       escalate_backoff: float) -> bool:
    """Whether a force-upload is still to be sent for the current stall.

    Only the escalation *decision* lives in watchdog_policy.should_escalate;
    this mirrors it for reporting, so a status consumer and the decision cannot
    disagree about whether an escalation is outstanding.
    """
    if stale_seconds < escalate_after:
        return False
    return not (
        last_escalation
        and (now - last_escalation) < escalate_backoff
    )


def is_escalated(stale_seconds: float, last_escalation: float, now: float, *,
                 escalate_after: float, escalate_backoff: float) -> bool:
    """Whether the integration currently counts as ESCALATED, for reporting.

    This is a LIVE state, not a session flag: it means the stall is still past
    the escalation threshold AND the force-upload for it has already been spent.
    Deriving it from ``bool(last_escalation)`` instead kept reporting
    "Escalated" for the rest of the session once one force-upload had gone out,
    even after normal data resumed - which is the bug this exists to prevent.

    Note the asymmetry with ``escalation_pending``: that one is true *before*
    the force-upload is sent, this one *after*. Both are false once data
    returns, because staleness clears.
    """
    if stale_seconds < escalate_after:
        return False
    return not escalation_pending(
        stale_seconds, last_escalation, now,
        escalate_after=escalate_after,
        escalate_backoff=escalate_backoff,
    )


def describe(
    *,
    connected: bool,
    active: bool,
    silence_seconds: float,
    silence_threshold: float,
    escalated: bool,
) -> str:
    """The one-word answer to "what is it doing?".

    Ordered by how much the answer matters: a down socket outranks a stall,
    which outranks the cadence. `escalated` only distinguishes SILENT from
    ESCALATED — both mean nothing is arriving; one means we have already tried
    the only thing that works.
    """
    if not connected:
        return RECONNECTING
    if silence_seconds >= silence_threshold:
        return ESCALATED if escalated else SILENT
    return ACTIVE if active else PARKED


def format_age(seconds: float) -> str:
    """A human age, with second precision below a minute.

    Minute granularity is wrong for anything on a sub-minute cycle, and the
    credential refresh loop is exactly that: it mints a new MQTT password every
    `mqtt_refresh_interval` (45s by default), so the age is a sawtooth between 0
    and 45 seconds and rounds to "0m" forever. The sensor then looks dead while
    working perfectly — the state never changes, so Home Assistant reports a
    `last_changed` an hour old even as `age_seconds` climbs and resets every
    minute and a half.

    Seconds below a minute make that cycle visible, which is the point of the
    entity: a healthy refresh loop shows a number that keeps resetting, and a
    stalled one shows a number that keeps climbing. Above a minute the exact
    seconds stop mattering and the coarser form is easier to read.
    """
    age = int(seconds)
    if age < 0:
        age = 0
    if age < 60:
        return f"{age}s"
    hours = age // 3600
    minutes = (age % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
