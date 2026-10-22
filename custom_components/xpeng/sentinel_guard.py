"""Recognise a tyre-pressure reading that means "the sensor has not answered".

Measured on the live install (.88), 2026-10-17/18: the first state frame after
the car is woken carries ``instant.tpms`` with all four pressures at ``0`` -
and, in the same frame, ``hvac.hvac_inner_temp_float`` at ``87.5``. The next
frame, one to two seconds later, carries the real values. Home Assistant
recorded the zeros, so a tyre pressure dropped to 0 kPa and reinflated two
seconds later, at 03:00:25, 06:22:36 and 08:03:39 on 18 Oct and 05:57:15 on
19 Oct (Australia/Sydney). The first three each follow a credential renewal by
33-61 s - a session rebuild, which sends the wake - and the fourth follows a
stalled fan-out being escalated with the same kind of force-upload. Every
dashboard drawing that history showed a tyre going flat.

A TPMS sensor that has not answered yet is encoded as 0, not as "absent", and
four wheels are never at 0 kPa at once on a car that is parked or driving. So a
pressure of 0 is "not reported" and must not overwrite the last real reading. A
*low* pressure is still a reading and is published unchanged - only 0, or a
negative (equally impossible), is treated as missing.

Carrying the previous reading forward, rather than publishing ``unknown``,
keeps the behaviour the README promises - an entity holds its last known value
when the car does not report - and keeps the zero out of the recorder and out
of the TeslaMate provider, which maps this same state tree.

Deliberately no Home Assistant import, like replay_guard.py and
watchdog_policy.py: the decision is small and is tested without a car.
"""

from __future__ import annotations

from typing import Any

#: The four pressures in ``instant.tpms``.
TPMS_PRESSURE_KEYS: tuple[str, ...] = (
    "tpms_pressure_fl",
    "tpms_pressure_fr",
    "tpms_pressure_rl",
    "tpms_pressure_rr",
)


def is_unreported(value: Any) -> bool:
    """Whether *value* is a TPMS placeholder rather than a pressure.

    ``0``, a negative, an empty string and ``None`` all mean "no reading yet".
    Anything that is not recognisably a number is left alone: the point of this
    module is to drop the one value that is provably impossible, not to
    second-guess a payload shape it has never seen.
    """
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value <= 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        try:
            return float(text) <= 0
        except ValueError:
            return False
    return False


def _previous_tpms(previous_content: Any) -> dict[str, Any]:
    """The ``instant.tpms`` dict of the last accepted state, or an empty one."""
    if not isinstance(previous_content, dict):
        return {}
    instant = previous_content.get("instant")
    if not isinstance(instant, dict):
        return {}
    tpms = instant.get("tpms")
    return tpms if isinstance(tpms, dict) else {}


def with_previous_tpms(instant: Any, previous_content: Any = None) -> Any:
    """Return *instant* with unreported pressures replaced by the last real ones.

    *previous_content* is the ``msg_content`` of the last accepted state; the
    last real pressure is carried forward from it. When the car was never heard
    from, the placeholder is dropped instead, so the sensor reads unknown
    rather than 0.

    The same *instant* object comes back when every reading is real, so the
    caller can skip rebuilding the state by identity alone.
    """
    if not isinstance(instant, dict):
        return instant
    tpms = instant.get("tpms")
    if not isinstance(tpms, dict):
        return instant

    previous = _previous_tpms(previous_content)
    repaired: dict[str, Any] = {}
    replaced = False
    for key, value in tpms.items():
        if not is_unreported(value):
            repaired[key] = value
            continue
        replaced = True
        carried = previous.get(key)
        if not is_unreported(carried):
            repaired[key] = carried
    if not replaced:
        return instant

    result = dict(instant)
    if repaired:
        result["tpms"] = repaired
    else:
        # Nothing has ever been reported for this car: absent, not 0.
        result.pop("tpms", None)
    return result
