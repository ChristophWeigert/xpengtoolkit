# Templates

Templates are used for derived values the integration does not create itself. The
integration already covers a lot of ground — the charging state, the estimated
time to full, the parked-consumption estimate and the service countdown all exist
as sensors — so the useful templates are the ones that combine them with
something of your own, such as a tariff.

::: tip
Give every template a `unique_id`. Without one it cannot be edited from the UI and
you will end up reading the YAML every time.
:::

## Range per percent

Useful for spotting a pack that is losing capacity: the figure should hold steady
for months, then drift.

```yaml
template:
  - sensor:
      - name: "XPENG km per percent"
        unique_id: xpeng_km_per_percent
        unit_of_measurement: "km/%"
        state_class: measurement
        availability: >-
          {{ states('sensor.xpeng_<vehicle>_battery') | is_number
             and states('sensor.xpeng_<vehicle>_battery') | float(0) > 0 }}
        state: >-
          {{ (states('sensor.xpeng_<vehicle>_range') | float(0)
              / states('sensor.xpeng_<vehicle>_battery') | float(1)) | round(2) }}
```

## What the last charge cost

`sensor.xpeng_<vehicle>_last_charge_energy` is the energy the last session took,
in kWh. Multiply it by a tariff you keep in a helper.

```yaml
input_number:
  electricity_tariff:
    name: Electricity tariff
    min: 0
    max: 1.5
    step: 0.001
    unit_of_measurement: "$/kWh"

template:
  - sensor:
      - name: "XPENG last charge cost"
        unique_id: xpeng_last_charge_cost
        unit_of_measurement: "$"
        state: >-
          {{ (states('sensor.xpeng_<vehicle>_last_charge_energy') | float(0)
              * states('input_number.electricity_tariff') | float(0)) | round(2) }}
        attributes:
          energy_kwh: "{{ states('sensor.xpeng_<vehicle>_last_charge_energy') }}"
          range_added_km: "{{ states('sensor.xpeng_<vehicle>_last_charge_range_added') }}"
```

## Efficiency of the last charge

Kilometres added per kWh taken. A DC session normally reads better than an AC
one, because the AC figure carries the onboard charger's losses.

```yaml
template:
  - sensor:
      - name: "XPENG last charge efficiency"
        unique_id: xpeng_last_charge_efficiency
        unit_of_measurement: "km/kWh"
        state_class: measurement
        availability: >-
          {{ states('sensor.xpeng_<vehicle>_last_charge_energy') | float(0) > 0 }}
        state: >-
          {{ (states('sensor.xpeng_<vehicle>_last_charge_range_added') | float(0)
              / states('sensor.xpeng_<vehicle>_last_charge_energy') | float(1)) | round(2) }}
```

## Service due soon

`next_maintenance` carries its own attributes, so this only has to decide whether
the answer is soon.

```yaml
template:
  - binary_sensor:
      - name: "XPENG service due"
        unique_id: xpeng_service_due
        device_class: problem
        state: >-
          {{ state_attr('sensor.xpeng_<vehicle>_next_maintenance', 'days_remaining') | int(999) < 30 }}
```

## A summary card

A markdown card is often the clearest way to show a car: one line per fact, no
card chrome.

```yaml
type: markdown
content: >-
  ## {{ states('sensor.xpeng_<vehicle>_vehicle_identity') }}

  **{{ states('sensor.xpeng_<vehicle>_battery') }}%** —
  {{ states('sensor.xpeng_<vehicle>_range') }} km

  {% if is_state('binary_sensor.xpeng_<vehicle>_charging', 'on') %}
  Charging at {{ states('sensor.xpeng_<vehicle>_charge_power') }} kW,
  {{ states('sensor.xpeng_<vehicle>_charge_time_remaining') }} min to the limit
  {% else %}
  {{ states('sensor.xpeng_<vehicle>_charging_state') }}
  {% endif %}

  Doors {{ states('binary_sensor.xpeng_<vehicle>_doors') }} ·
  {{ states('sensor.xpeng_<vehicle>_odometer') }} km ·
  data {{ relative_time(states.sensor.xpeng_<vehicle>_last_data_timestamp.last_changed) }} ago
```

::: warning
The energy-consumption sensor reads exactly `0.0` on a non-owner (shared)
account, because XPENG gates that endpoint — it is not a measurement of zero. Do
not build a template that treats it as real data; check the
`zero_may_mean_restricted` attribute the sensor carries instead. See
[Entities](../reference/entities).
:::

## Templates in automations

A template condition is often better than a state trigger when the interesting
moment is a combination of two entities — here, the car being plugged in and the
cheap window starting.

```yaml
- alias: XPENG charge in the cheap window
  triggers:
    - trigger: time
      at: "01:00:00"
  conditions:
    - condition: state
      entity_id: binary_sensor.xpeng_<vehicle>_charging
      state: "off"
    - condition: template
      value_template: >-
        {{ states('sensor.xpeng_<vehicle>_charging_state') in ['Connected', 'Scheduled'] }}
  actions:
    - action: xpeng.start_charge
```
