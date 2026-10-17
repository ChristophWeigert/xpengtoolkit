"""Whether the credential-refresh loop is healthy — as arithmetic.

A dead credential does not announce itself. Every vehicle entity simply stops
changing, and a frozen sensor looks exactly like a parked car: this integration
is push-based, so "nothing arrived" is the *normal* state of an asleep vehicle.
``XPengCredentialsValidBinarySensor`` exists to tell those two apart, and this
module holds the part of that answer which is arithmetic. It has no Home
Assistant import, so it can be exercised in a bare Python interpreter — and
because the sensor calls the function below rather than reimplementing it, the
rule that is tested is the rule that ships.

Two deliberate choices:

* **Three refresh intervals, not one.** ``createMqttAccount`` fails
  transiently (a 5xx, a dropped connection, a refresh overlapping the timer),
  and one miss must not declare the credentials dead. ``3 * interval`` tolerates
  two consecutive misses at the 45s default and still flips within ~135s.
* **A known failure count beats staleness in the reason.** When
  ``consecutive_failures`` is non-zero the broker has actually refused, which
  names the cause; "no successful refresh for Ns" only describes the symptom.
  Either way the entity is off.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

#: How many refresh intervals may pass with no successful refresh before the
#: credentials in hand are called stale. See the module docstring.
STALE_INTERVALS = 3

#: The loop is doing its job.
REASON_OK = "ok"

#: A live MQTT session is running with no failure recorded. With mint-on-demand
#: (the app's model) there are deliberately no successful mints while connected,
#: so "last mint age" alone would call a perfectly healthy session stale.
REASON_CONNECTED = "connected, no failures recorded"

#: Nothing has succeeded yet this session — a fresh start, or a login that has
#: never once worked. Distinct from stale, and worth wording differently: it
#: points at onboarding, not at a blip.
REASON_NEVER = "no successful refresh yet"


def as_float(value: object, default: float | None = None) -> float | None:
    """`value` as a finite float, else `default`.

    The inputs arrive from a live Home Assistant process — a dict written by
    another thread, options the user could have edited, timestamps that may be
    absent — so a diagnostic entity must degrade to "unknown" rather than raise
    while Home Assistant is writing its state. NaN and the infinities are
    rejected here because ``int()`` on them raises, which would defeat that.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def iso(epoch: float | None) -> str | None:
    """ISO 8601 (local offset) for an epoch, or None when there is no time.

    Local offset rather than a bare `Z`, matching what the rest of this
    integration's diagnostics show, so two timestamps in one dashboard can be
    compared without the reader doing the arithmetic.
    """
    number = as_float(epoch)
    if number is None:
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat()


def age_seconds(now: float, timestamp: float | None) -> int:
    """Whole seconds between `timestamp` and `now`, or 0 when unmeasurable.

    0 rather than None because both callers publish an `int`, and an absent
    timestamp there means "no message has arrived yet" — the same thing an age
    of 0 from a freshly constructed client means, which is a real ambiguity in
    the MQTT client's own clocks. It is not a claim that a message just arrived;
    `last_data_update` (from the coordinator) is the entity that answers "has
    vehicle data ever arrived?" without that ambiguity.

    A negative result is possible if the clock steps backwards or the timestamp
    is in the future, and is deliberately not clamped: a negative age is the
    visible tell for a broken clock, and hiding it would hide the bug.
    """
    moment = as_float(timestamp)
    reference = as_float(now)
    if moment is None or reference is None:
        return 0
    return int(reference - moment)


def credentials_health(
    now: float,
    last_success: float | None,
    consecutive_failures: int,
    refresh_interval: int,
    connected: bool = False,
) -> dict:
    """Decide whether the MQTT credential loop is healthy right now.

    ``now`` is a parameter rather than ``time.time()`` inside, so the decision is
    deterministic and testable at any age without sleeping.

    Returns ``{"valid", "age_s", "stale_after_s", "reason"}``:

    * ``stale_after_s`` — ``3 * refresh_interval``.
    * ``valid`` — True when a live connection exists with no recorded failure;
      otherwise only when something has succeeded, that success is no older than
      ``stale_after_s``, and nothing has failed since. The credential is minted on
      demand before a connection now (the app's model), so while a session is up
      there are *no* recent mints to point at — a healthy connection is its own
      evidence, and without that rule the renewal app's validity poll would see
      "stale" and renew every few minutes, evicting the very session it saw.
    * ``reason`` — a short phrase for the entity's attributes, because an off
      sensor with no explanation is the complaint this whole feature answers.
    """
    interval = as_float(refresh_interval, 0.0) or 0.0
    stale_after_s = int(STALE_INTERVALS * interval)

    failures = int(as_float(consecutive_failures, 0.0) or 0.0)

    success = as_float(last_success)

    if connected and failures == 0:
        # Mint-on-demand: nothing is minted while a session is live, so a fresh
        # success timestamp cannot be the test. A working connection with no
        # refusal on record is.
        return {
            "valid": True,
            "age_s": (
                int(as_float(now, 0.0) - success) if success is not None else None
            ),
            "stale_after_s": stale_after_s,
            "reason": REASON_CONNECTED,
        }
    if success is None:
        return {
            "valid": False,
            "age_s": None,
            "stale_after_s": stale_after_s,
            "reason": REASON_NEVER,
        }

    age_s = int(as_float(now, 0.0) - success)

    if failures > 0:
        # Not `>` on the interval: a failure is an observation, staleness is an
        # inference, and the observation is the one that names the cause.
        return {
            "valid": False,
            "age_s": age_s,
            "stale_after_s": stale_after_s,
            "reason": f"{failures} consecutive failures",
        }

    if age_s > stale_after_s:
        return {
            "valid": False,
            "age_s": age_s,
            "stale_after_s": stale_after_s,
            "reason": f"no successful refresh for {age_s}s",
        }

    return {
        "valid": True,
        "age_s": age_s,
        "stale_after_s": stale_after_s,
        "reason": REASON_OK,
    }
