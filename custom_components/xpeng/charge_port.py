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
CHARGING_STATES: Final = {
    0: "Disconnected",
    1: "Connected",
    2: "Charging",
    3: "Complete",
    4: "Error",
    8: "Idle",
    15: "Scheduled",
}

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
