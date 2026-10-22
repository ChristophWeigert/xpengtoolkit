# Using this integration as TeslaMate's API provider

[TeslaMate](https://github.com/teslamate-org/teslamate) is a self-hosted Tesla
logger. Since 4.x it can be pointed at a third-party API provider instead of
Tesla, through five environment variables: `TOKEN`, `TESLA_API_HOST`,
`TESLA_AUTH_HOST`/`TESLA_AUTH_PATH` and `TESLA_WSS_HOST`.

This integration can be that provider. Enable it and TeslaMate logs the XPENG
exactly as it logs a Tesla: drives, charging sessions, positions, battery and
range history, state changes and the Grafana dashboards - all sourced from the
live MQTT telemetry the integration already receives. TeslaMate never talks to
XPENG, and this integration never talks to TeslaMate's database.

**Read-only.** Only TeslaMate's four read endpoints and its streaming socket are
served. There is no command route, so pointing TeslaMate here cannot lock,
unlock, charge, move or wake the car.

## What you get, and what is approximate

| TeslaMate | Source in the integration | Fidelity |
| --- | --- | --- |
| Drives, distance, speed | `interval.drive.speed`, `latitude`, `longitude` | exact (km/h and km converted to Tesla's mph/miles) |
| Drive start/end | `instant.drive.shift_state` / `ext.vehicleMode` | exact |
| Charging sessions | `instant.charge.charging_state` + `interval.charge.power` | exact start/stop |
| Charge energy added | battery % change x configured usable kWh | **estimated** - XPENG does not publish the live session's energy |
| Charging power, voltage, current | `interval.charge.*` | exact |
| Battery %, range | `instant.charge.battery_soc`, `odometer.avalible_driving_distance` | exact |
| Odometer | `instant.odometer.total_mileage` | exact |
| Inside temperature | `instant.hvac.hvac_inner_temp_float` | exact |
| Outside temperature | the weather-at-vehicle REST value | **approximate** - ambient weather, not a car sensor |
| Tyre pressures | `instant.tpms.*` (kPa -> bar) | exact |
| Lock, sentry, doors, boot/bonnet | `instant.door.*`, `instant.ext.soldier_camera_status` | exact |
| Software version | the integration's XOS version | exact |
| Climate, seat heat, steering-wheel heat | `instant.hvac.*`, `instant.seat.*` | exact |
| Drive power | not exposed by XPENG | reported as `0` while driving, negative charger power while charging |
| Elevation / ascent / descent | not exposed by XPENG | not recorded (TeslaMate's terrain fallback needs no stream; we stream) |
| Fast-charger flag | `interval.charge.power >= 20 kW` | **heuristic** - used for TeslaMate's charge-curve labels |

A TeslaMate "vehicle" is one TeslaMate instance per car. If your account has two
XPENGs, each is a separate config entry here; run one TeslaMate per car (or wait
for the multi-car aggregation noted under *Known limits*).

## Enable it

1. **Settings -> Devices & services -> Xpengtk -> Configure.**
2. Tick **Enable the TeslaMate API provider**. Leave the port at `8200` unless it
   is taken; do not use `8123`, that is Home Assistant itself.
3. The **TeslaMate provider token** field already holds a generated secret.
   Copy it now - this is the `TOKEN` TeslaMate will send.
4. Save. The integration reloads and logs
   `TeslaMate provider listening on port 8200 of 0.0.0.0`.

The port must be reachable from wherever TeslaMate runs. **Home Assistant OS**
and **Supervised** run core on the host network, so `8200` is already reachable
at the host's IP. **Home Assistant Container** publishes ports explicitly - add
`8200:8200` to that container's port list and restart it before continuing.

Check it from the machine running TeslaMate:

```bash
curl http://<HA-host>:8200/healthz
```

A JSON body with `"ok": true` and the car's VIN tail means the port is reachable.
The health endpoint needs no token; every other endpoint does.

## Point TeslaMate at it

Add these to TeslaMate's environment (the same place `ENCRYPTION_KEY` lives):

```yaml
- TOKEN=<the token from the options screen>
# API + token endpoints. Plain http is supported and is the normal case here.
- TESLA_API_HOST=http://<HA-host>:8200
- TESLA_AUTH_HOST=http://<HA-host>:8200
- TESLA_AUTH_PATH=/oauth2/v3
# Optional but recommended: the old-style streaming socket. Without it TeslaMate
# polls vehicle_data every few seconds while driving; with it, positions arrive
# as fast as the car reports them and drive detection is instant.
- TESLA_WSS_HOST=ws://<HA-host>:8200
```

Restart TeslaMate, open its web UI and click **Sign in** once. The sign-in page
shows "You are using the API key ... provided by ..."; the button is all that is
needed - there is nothing to paste there. TeslaMate then lists the car and starts
a logger. Its log should show:

```
Starting logger for 'G6'
```

TeslaMate needs HTTPS for the *Tesla cloud*, but a plain-`http` provider is
supported: its HTTP client drops the TLS 1.3 connection options for `http://`
pools (Finch's `maybe_drop_tls_options/2`). Do not put a self-signed HTTPS proxy
in front - TeslaMate has no environment variable to accept an invalid API
certificate, while the streaming socket does (`TESLA_WSS_TLS_ACCEPT_INVALID_CERTS`).
If you terminate TLS properly with a trusted certificate, `https://`/`wss://`
work as well.

## XpengMate - the XPENG-native fork

[XpengMate](https://github.com/xpengtoolkit/xpengmate) is a rebranded fork of
TeslaMate with the Tesla data path replaced by an XPENG one. It keeps the database
schema, the MQTT/Home Assistant integration and the dashboards, and adds a history
browser, EVConduit backfill, XPENG GDPR export import and an in-app settings page.
Its recommended live source is *this provider*: set `XPENG_SOURCE=provider` and
point `XPENG_PROVIDER_UPSTREAM_URL`/`XPENG_PROVIDER_UPSTREAM_TOKEN` at port 8200,
so Home Assistant stays the only owner of the XPENG account session. It also reads
`polling_status` to report a parked car as `asleep`, which is what lets
TeslaMate's own sleep detection work.

Everything above applies to it unchanged - only the place the provider
environment variables are set differs.

## How the car's state is decided

TeslaMate asks for `vehicle_data` only while it believes the car is reachable,
and otherwise polls the cheap vehicle object every 30 seconds. The provider maps
that onto XPENG's push-based world:

* **Driving or charging -> `online`.** Always, regardless of how old the last
  frame is.
* **Parked with real data in the last two hours -> `online`.** TeslaMate keeps
  logging and, after a few idle minutes, moves to its own `suspended` state - it
  then polls only every 12 minutes and waits for the stream.
* **No real data for two hours -> `asleep`.** The integration's passive parked
  query is hourly and a sleeping car answers it with a stub that carries nothing,
  so this is the honest "the car is asleep, stop asking" signal.
* **Cloud socket down -> `offline`.**

While the car is `online` the streaming socket sends a `data:update` frame on
every telemetry update plus a heartbeat every 15 seconds (TeslaMate drops a
silent stream after 30). While it is `asleep` the stream goes quiet on purpose,
which is exactly how TeslaMate detects a sleeping Tesla.

## What is written to TeslaMate

The provider translates to the shapes TeslaMate's car process requires:

* `state` is only `online`, `asleep` or `offline`.
* `drive_state.shift_state` is only `P`, `R`, `N`, `D` or null - never `""`.
* Distances and speed arrive in **miles** and **mph**, temperatures in **°C**,
  tyre pressures in **bar**: TeslaMate converts those once when it writes the
  database. Sending km would silently be off by 1.609x in every dashboard.
* Every `vehicle_data` response has all five state blocks, a millisecond
  timestamp and a numeric latitude/longitude. TeslaMate discards a response
  missing a block ("incomplete fetch result") and crashes the car's logger on a
  missing timestamp or position, so the provider carries the last known position
  forward and never lets a timestamp go backwards.
* `vehicle_config` is always present even though XPENG is not a Tesla: TeslaMate
  matches `{:ok, attrs} = identify(vehicle)` on every online transition.

## Known limits

* **Read-only, and no waking.** TeslaMate does not send commands, so no command
  support was added. The provider also cannot wake a sleeping car for a poll; it
  waits for the integration's own cadence and for the car to report.
* **One car per config entry / TeslaMate instance.** The provider serves the VIN
  of the entry that started it. Two entries both enabled on the same port: the
  second fails to bind and logs an error, and the first serves its car.
* **`charge_energy_added` is an estimate.** It is integrated from the battery
  percentage against the **Usable battery capacity** option. If that option is
  wrong the session's kWh figure is proportionally wrong (TeslaMate's own
  efficiency numbers inherit that). It is smooth and never negative, and a
  session is held open across the car's mid-session `charging_state` blips for
  120 s so one charge does not become two.
* **Outside temperature is weather, not a sensor.** It comes from XPENG's
  weather-at-vehicle service.
* **No drive power or elevation.** XPENG does not expose motor power to this
  integration; TeslaMate shows `0` kW in drive positions. Elevation, ascent and
  descent stay empty.
* **Fast/slow charging is inferred** from power above 20 kW for TeslaMate's
  charge-curve grouping.
* **The provider is a second HTTP server inside Home Assistant**, on its own
  port. It starts and stops with the config entry (options save -> reload).

## Security

* The provider is off by default and refuses to start without a token of at least
  eight characters.
* Every endpoint except `/healthz` requires `Authorization: Bearer <token>`; the
  streaming socket authenticates in its subscribe frame (TeslaMate's websocket
  client cannot send a header).
* It is read-only: car location and state, no control.
* Treat the token as a password: anyone on your network with it can read where
  the car is. The provider speaks plain HTTP, so keep it on a trusted LAN or put
  it behind a TLS-terminating reverse proxy with a trusted certificate.
* Changing the token in the options screen means TeslaMate's stored access token
  is now wrong. TeslaMate treats that as an expired session: it deletes its
  stored tokens and asks you to sign in again. Update `TOKEN` in TeslaMate's
  environment first, then sign in again.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Could not start the TeslaMate provider on port 8200` in the HA log | The port is taken. Pick another in the options and update `TESLA_API_HOST`/`TESLA_AUTH_HOST`/`TESLA_WSS_HOST`. |
| TeslaMate's sign-in says "Tokens are invalid" | `TOKEN` in TeslaMate does not match the options screen. Fix `TOKEN`, restart, sign in again. |
| `/healthz` works from the HA host but not from the TeslaMate container | Container networking. Use the HA host's LAN IP (not `localhost`), and make sure the port is published/reachable; `network_mode: host` on the TeslaMate services is the simplest fix on Linux. |
| No car appears in TeslaMate | The car has never reported telemetry. The provider reports `offline` until the first MQTT frame arrives (the integration is silent on a brand-new install with no first state). Wait for the first push, or drive the car once. |
| Drives appear but distance is wrong by ~60% | Something is double-converting units. This provider is meant to serve **miles**; if you patched it to serve km, undo that. |
| One charge shows as several sessions | The car's `charging_state` is flapping. The 120 s grace period covers the measured 2<->15 alternation; if your car flaps more slowly, extend `CHARGING_GRACE_SECONDS` in `teslamate_provider.py`. |
| `address is already in use` on every start | Same as the first row; note that an options save reloads the integration, so a manual `homeassistant.reload_config_entry` is not needed. |

## For maintainers

The provider is two modules and is deliberately Home-Assistant-free in its pure
half:

* `custom_components/xpeng/teslamate_mapping.py` - pure functions from a
  `Telemetry` snapshot to Tesla JSON, units, state and id derivation. No HA
  import, so it is tested with plain `python3`.
* `custom_components/xpeng/teslamate_provider.py` - the aiohttp server, token
  middleware, streaming fan-out, and the per-car bookkeeping the pure half cannot
  hold (monotonic timestamps, last known position, charge-session energy).
  Started from `__init__.py` only when the option is on; stopped on unload.

Contract summary (all responses `application/json`):

| Method | Path | Body |
| --- | --- | --- |
| POST | `/oauth2/v3/token` | `{"access_token", "refresh_token", "expires_in": <int>, "token_type"}` |
| GET | `/api/1/products` | `{"response": [vehicle, ...], "count": n}` |
| GET | `/api/1/vehicles/{id}` | `{"response": vehicle}` |
| GET | `/api/1/vehicles/{id}/vehicle_data` | `{"response": vehicle with five state blocks}` |
| GET | `/streaming/` | websocket: `data:subscribe_oauth` in, `data:update` out |
| GET | `/healthz` | diagnostics, no auth |

`{id}` is the integer `id` from `/products` (TeslaMate stores it as `cars.eid`).
TeslaMate's streaming client subscribes with the column list
`speed,odometer,soc,elevation,est_heading,est_lat,est_lng,power,shift_state,range,est_range,heading`
and expects one comma-separated `value` with `time` first, in that order.

Tests:

```bash
python3 tests/test_teslamate_mapping.py      # no dependencies

# The HTTP/websocket contract test needs aiohttp (Home Assistant ships it):
python3 -m venv /tmp/teslamate-test && /tmp/teslamate-test/bin/pip install aiohttp
/tmp/teslamate-test/bin/python tests/test_teslamate_provider.py
```
