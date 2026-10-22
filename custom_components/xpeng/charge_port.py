"""What the charge port and its cable are doing, from fields that can say.

Pure - no Home Assistant import - because the answer is a mapping and the mapping
is the part worth testing. ``const.py`` imports these into the sensor and binary
sensor descriptions.

The problem this exists for, measured on a G6 (export car, single CCS2 port) on
2026-10-17 with **no cable attached** and the car awake for nine hours:

    slow_charge_port_status = 2   ("Connected", permanently, cable or no cable)
    fast_charge_port_status = 2   (the same, always - this is why there is no
                                   fast-port sensor on export cars)
    charge_gun_status       = 0   (the physical connector: empty)
    charge_connector_status = 7   (that car's "unknown"; the digital vehicle
                                   block answers 0 for the same physical state)
    charging_state          = 15  (Scheduled - not a cable-present code)

``binary_sensor.plugged_in`` read ON for as long as it trusted the port field, and
``sensor.slow_charge_port`` read "Connected" the whole time. Both now need the gun
to see a cable.

The charge schedule, measured on 2026-10-17 by toggling it in the app:

    scheduler off:  repeat_type = 0, reserve_charge_start_time = '22:00'
    scheduler on:   repeat_type = 2, reserve_charge_start_time = '22:00'
                    reserve_charge_end_time   = '31:63'

The app's own words for the plan were "scheduled AC charging, 22:00, charge to
charge limit" - there is no end time, which is why the end field holds 31:63 and
must not be shown as a time. ``repeat_type`` is the field that says the plan is
armed, so that - not the charging-state code - is what "Scheduled" is read from.

(``charging_state`` stayed 15 across that toggle, which is what 0.6.4 concluded from
it being 15 with the scheduler off: 15 is not the scheduler's code.)

And a second measurement, the one that decides ``charge_cable_state`` below. The car
reports the cable only while the session is awake:

    plugged in, waiting for the plan (asleep):   gun 0   connector 7   ports 2/2   state 15
    unplugged, hours earlier:                    gun 0   connector 7   ports 2/2   state 15
    plugged in, session awake (plan switched off): gun 4  connector 7   port 0      state 1

The first two rows are the same payload. So for an armed plan the car's silence is
indistinguishable from no cable, and ``plugged_in`` says so rather than guessing "off".
"""

from __future__ import annotations

from typing import Any, Final

# Charge port status mapping. Measured on a G6 2026-10-16 against the physical
# port: 0 while the flap is shut with no cable, 1 while the flap is open with
# nothing in it, 2 while the AC cable is inserted.
CHARGE_PORT_STATES: Final = {
    0: "Closed",
    1: "Open",
    2: "Connected",
}

#: Charging state mapping, kept beside the port states because the plugged test
#: needs to know which of these codes cannot happen without a cable.
#:
#: There is deliberately no 15 here. 15 is what this car reports with the app's
#: charge scheduler switched off and nothing plugged in - for nine hours - and it
#: also alternates with 2 mid-session (measured 2026-10-16 12:07, charge power
#: 5.2 kW). It is not "Scheduled", which is what it used to be labelled, and no
#: other label could be justified from the payload alone. ``charging_state_label``
#: answers 15 from the cable instead.
CHARGING_STATES: Final = {
    0: "Disconnected",
    1: "Connected",
    2: "Charging",
    3: "Complete",
    4: "Error",
    8: "Idle",
}

#: The one charging-state code with no trustworthy label of its own.
CHARGING_STATE_UNLABELLED: Final = 15

#: The one charge-port value that is supposed to mean a cable is in the port.
CHARGE_PORT_CONNECTED: Final = 2

#: ``charge_gun_status`` with nothing in the port. Measured on a G6 on
#: 2026-10-17, car unplugged, and stable across nine hours of pushes. The plugged
#: value was not captured on that car, so the test below is "anything but empty":
#: a car with a cable in it must never read "not plugged", and that is the error
#: worth risking here rather than the reverse.
CHARGE_GUN_EMPTY: Final = 0

#: ``charge_connector_status`` values that mean a connector is present. The same
#: car reports 7 with nothing attached - its "unknown", which the digital vehicle
#: block answers with 0 - so neither 0 nor 7 is evidence of a cable.
CHARGE_CONNECTOR_ATTACHED: Final = (1, 2, 3, 4, 5, 6)

#: The ``charging_state`` codes that cannot occur without a cable: Connected,
#: Charging, Complete.
CHARGING_STATES_PLUGGED: Final = (1, 2, 3)

#: The states ``sensor.charging_state`` may report: everything the car's own code
#: maps to, plus "Scheduled", which is derived from the charge plan rather than
#: from the code (see ``charging_state_label``).
CHARGE_STATE_OPTIONS: Final = (*CHARGING_STATES.values(), "Scheduled")

#: ``repeat_type`` with the charge plan switched off. Measured 2026-10-17: 0 with
#: the app's scheduler off, 2 with it on. The test is "truthy", not "== 2", so an
#: unobserved non-zero repeat pattern still counts as armed.
CHARGE_SCHEDULE_OFF: Final = 0


def charge_cable_attached(charge: Any) -> bool:
    """Is a charge cable attached?

    The gun and connector fields carry the physical evidence, so they decide. The
    port field is consulted only for a car that reports neither: the field set is
    per-model, and an older car without gun fields should not lose the sensor
    (``plugged_in`` existed for those cars first).

    Note the one honest gap: the value the gun takes with a cable in it has not
    been observed on this car, so "attached" is anything but empty. That errs
    towards reporting plugged; reporting *not* plugged while a car is charging
    would be worse.
    """
    if not isinstance(charge, dict):
        return False
    # A car that says it is charging, has finished charging, or is connected has
    # a cable in it, whatever the other fields claim.
    if charge.get("charging_state") in CHARGING_STATES_PLUGGED:
        return True
    gun = charge.get("charge_gun_status")
    connector = charge.get("charge_connector_status")
    if gun is None and connector is None:
        # No physical field on this car at all: fall back to the port field, the
        # signal this sensor was built on.
        return charge.get("slow_charge_port_status") == CHARGE_PORT_CONNECTED
    if gun is not None and gun != CHARGE_GUN_EMPTY:
        return True
    return connector in CHARGE_CONNECTOR_ATTACHED


#: Past this much silence from the car, "no cable" stops being evidence.
#:
#: While the car is awake it reports at least once per parked query (15 minutes by
#: default), so a charge section this old predates the car going to sleep - and a
#: sleeping car has said nothing about a cable plugged in since. Only matters when
#: a plan is armed (see below); a car with no plan starts charging the moment it is
#: plugged, which wakes it and makes it report.
CHARGE_CABLE_EVIDENCE_MAX_AGE: Final = 20 * 60


def charge_cable_state(
    charge: Any, *, silent_seconds: float | None = None
) -> bool | None:
    """Is a cable attached - True, False, or None when the car cannot say.

    ``charge_cable_attached`` answers about positive evidence; this answers the
    question the binary sensor is actually asked, and it refuses the third case.
    Measured 2026-10-17: with the charge plan armed and the car asleep, plugged and
    unplugged produce byte-for-byte the same charge section (gun 0, connector 7,
    ports 2/2, state 15). Once the session woke - the plan was switched off - the
    same car reported gun 4 and state 1.

    ``silent_seconds`` is how long the car has gone without saying anything real
    (``poll_status['meaningful_silence_seconds']``). It is what keeps an armed plan
    from being a permanent excuse for "unknown": while the car is still talking its
    "no cable" is evidence, and it only becomes silence - and therefore unknown -
    once the car has stopped reporting. The plan is still required for the unknown,
    because a plan is what makes this car wait quietly in the first place.
    """
    if not isinstance(charge, dict):
        return None
    if charge_cable_attached(charge):
        return True
    if not charge_schedule_armed(charge):
        return False
    if silent_seconds is None or silent_seconds > CHARGE_CABLE_EVIDENCE_MAX_AGE:
        return None
    return False


def charge_port_state(charge: Any) -> str | None:
    """The charge port's display state, from whichever field is evidence.

    ``Connected`` requires the gun to see a cable (see
    ``charge_cable_attached``). While the gun reports nothing in the port, the
    port field still separates Closed from Open - except that its value 2 is
    discarded, because that is the one this car reports permanently.
    """
    if not isinstance(charge, dict):
        return None
    if charge_cable_attached(charge):
        return CHARGE_PORT_STATES[CHARGE_PORT_CONNECTED]
    port = charge.get("slow_charge_port_status")
    if port is None:
        return None
    if port == CHARGE_PORT_CONNECTED:
        return CHARGE_PORT_STATES[0]
    return CHARGE_PORT_STATES.get(port, f"Unknown ({port})")

def charge_schedule_armed(charge: Any) -> bool:
    """Is the car's own charge plan switched on?

    Measured on 2026-10-17 by toggling it in the app: ``repeat_type`` was 0 with
    the scheduler off and 2 with it on, while ``charging_state`` stayed 15 either
    way. The plan's start time is legible (``reserve_charge_start_time``, "22:00",
    matching the app) but its end is not when the plan charges to the limit - the
    car reports ``31:63``, an hour and minute that do not exist - so nothing here
    reads the end as a time.
    """
    if not isinstance(charge, dict):
        return False
    repeat = charge.get("repeat_type")
    return repeat is not None and repeat != CHARGE_SCHEDULE_OFF


def charging_state_label(charge: Any) -> str | None:
    """What to display for a charge section's ``charging_state``.

    Every code but 15 is what it always was. 15 is answered from the physical
    evidence - the same test ``plugged_in`` uses - because its own meaning could
    not be pinned: it shows up with nothing plugged in *and* mid-session, and this
    car carries reserve times (22:00/06:00) even with the app's scheduler
    disabled, so "Scheduled" was a guess that read as a fact. With no cable the
    honest state is "Disconnected"; with a cable it is "Connected" (a session that
    is not drawing).
    """
    if not isinstance(charge, dict):
        return None
    code = charge.get("charging_state")
    if code is None:
        return None
    if code == CHARGING_STATE_UNLABELLED:
        if charge_schedule_armed(charge):
            # The plan is armed and the car is waiting for it. This is what the
            # app says too ("scheduled AC charging"), and it is now read from the
            # field that tracks the plan rather than guessed from the code.
            return "Scheduled"
        return (
            CHARGING_STATES[1]
            if charge_cable_attached(charge)
            else CHARGING_STATES[0]
        )
    return CHARGING_STATES.get(code, f"Unknown ({code})")


#: Options key for "this car has motorized charge-port controls". Mirrored as
#: ``const.CONF_CHARGE_PORT_CONTROLS`` (const.py imports this module), so the
#: pure helper and the config option cannot drift apart.
CHARGE_PORT_CONTROLS_OPTION = "charge_port_controls"


def controls_enabled(options: Any) -> bool:
    """Whether this install expects the charge-port commands to do anything.

    A manual flap answers the open/close commands with an error, or with
    nothing, and there is no field that says which kind of flap a car has - so
    this is the owner's answer, recorded as an option. It defaults to True, so
    every install that never touched the option keeps the controls it had.

    Reported against a P7+ (2026-10-22, DrazorV fork): charge-port control did
    not work on that car, and its owner has a manual flap. The buttons are not
    created when this is off; the port state sensor is unaffected.
    """
    if not isinstance(options, dict):
        return True
    value = options.get(CHARGE_PORT_CONTROLS_OPTION, True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)
