# evcc compatibility

Compact mode retains the integration's data interfaces for evcc. In particular,
the old **Climate** binary sensor remains available alongside the native climate
controller: evcc's `climater` reads `on`/`off`, while a climate controller reports
HVAC modes. Both use the same measured vehicle data; neither invents state after
a command.

| evcc field | Integration entity | Example using a renamed car prefix |
| --- | --- | --- |
| `soc` | Battery sensor | `sensor.xpeng_car_battery` |
| `range` | Range sensor | `sensor.xpeng_car_range` |
| `limitSoc` | Charge-limit number | `number.xpeng_car_charge_limit` |
| `climater` | Climate binary sensor | `binary_sensor.xpeng_car_climate` |
| `wakeup` | Wake-vehicle button | `button.xpeng_car_wake_vehicle` |
| `odometer` | Odometer sensor | `sensor.xpeng_car_odometer` |

Use the actual entity IDs shown in Home Assistant; renaming a device does not
rename its entities. The charging-state/power sensors, cable-presence sensor,
last-data-update timestamp and `xpeng.charging_hint` service are also retained.
Wake/control permissions still apply to commands; read-only telemetry remains.

If compact mode had previously removed the climate binary sensor, the next setup
registers its original unique ID again. Home Assistant can restore its former ID
and metadata from the deleted-entry registry. If that metadata has expired,
verify the new entity ID in evcc. No instance-specific helper is needed.

The native charging switch can be used as evcc's `start_charging` interface where
the vehicle API-only charger is deliberately configured. In that case evcc's
`stop_charging` field is left empty. A normal wallbox controls charging itself;
adding that optional vehicle-only path is not necessary for telemetry integration.

References: [upstream XPENG guide](https://xpengtoolkit.github.io/xpengtoolkit/guide/evcc),
[evcc Home Assistant vehicle template](https://docs.evcc.io/en/vehicles/home-assistant).
