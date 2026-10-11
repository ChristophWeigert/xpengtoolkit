# Home Assistant examples

The examples are in three groups:

- Lovelace cards, to show the car on a dashboard
- Automations, to act on a change
- Templates, which are the building blocks for both

## Config packages

Home Assistant's packages feature lets you keep each example in its own file
instead of growing `configuration.yaml`. These pages assume a
`/config/packages/` folder.

Read up on packages and specifically how to
[create a packages folder](https://www.home-assistant.io/docs/configuration/packages/#create-a-packages-folder).

The short version: this snippet makes Home Assistant load every file in
`/config/packages/` as if it were written inline.

```yaml
homeassistant:
  packages: !include_dir_named packages
```

## Entity names in these examples

Every entity id the integration creates starts with the vehicle's own suffix —
the last six characters of the VIN — so the ids on your install will not match
the ones printed here. `sensor.xpeng_<vehicle>_battery` is written with that
placeholder throughout; see [Entities](../reference/entities) for the full list,
and Developer Tools → States to read the real ids off your own car.

::: tip
`sensor.xpeng_<vehicle>_vehicle_identity` names the model, and
`sensor.xpeng_<vehicle>_remote_capabilities` lists what the account is allowed to
do — a quick way to confirm what your car and account actually support before
building an automation on it.
:::
