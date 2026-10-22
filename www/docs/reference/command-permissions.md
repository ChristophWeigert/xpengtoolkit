# Command permissions

The XPENG account this integration holds can unlock a car, so every install carries a
ceiling on what it is allowed to *do* - independent of who is asking. Every command
belongs to one **capability group**, and every group is set to **Full**, **Read only**
or **Disabled**.

The ceiling lives here rather than in Home Assistant because Home Assistant cannot
supply it. Every XPENG command is a **target-less service**: `xpeng.unlock` takes no
`entity_id`, so the per-entity `read`/`control` permissions Home Assistant checks for
`lock.unlock` never run for it. Home Assistant's own user and group permissions still
govern the entities you expose; this policy governs everything else - automations,
scripts, voice assistants, dashboards, long-lived tokens, and callers with no user at
all.

## The three states

| State | What it means |
| --- | --- |
| **Full** | The command runs. |
| **Read only** | The entity is created and keeps reporting state, and every command it can send is refused. For climate and the lock this renders properly: the controls are withdrawn rather than left to fail. A `button` has no state to keep, so for buttons Read only behaves as Disabled. |
| **Disabled** | The entities are not created, and the group's services are refused. |

## Presets

**Configure -> Command permissions** offers four presets, and *Custom* on the same
screen opens the per-group matrix.

| Preset | Groups left on | What it is for |
| --- | --- | --- |
| **Read only** | queries | Watching the car. Nothing actuates it. |
| **Safe** | + comfort, lights, securing | Comfort and locking, but the car cannot be opened or charged remotely. |
| **Safe Plus** *(default for a new entry)* | + charging, power | Watching, pre-conditioning and charging - the common case - with unlocking, the trunk and windows, OTA and raw commands left off. |
| **Full access** | everything | What the integration did before this option existed, including unlocking. |

An entry with **no stored policy at all resolves to Full access**, so an install that
predates the option keeps behaving exactly as it did; **Safe Plus** is only what a
*new* entry starts at. See [Upgrading](#upgrading).

## Capability groups

| Capability group | Services | Entities |
| --- | --- | --- |
| **Queries and data** | `capture_snapshot`, `check_ota`, `clear_stuck_commands`, `force_calendar_sync`, `force_update`, `list_hvac_schedules`, `send_navigation` | `button.*_wake_vehicle`, `button.*_force_update`, `button.*_query_x_guard` |
| **Comfort and climate** | `climate_off`, `climate_on`, `create_hvac_schedule`, `defrost_off`, `defrost_on`, `delete_hvac_schedule`, `update_hvac_schedule` | `climate.*_climate`, `switch.*_front_defrost`, `number.*_fan_speed`, the four seat `select`s and `select.*_steering_wheel_heating`, plus one switch per HVAC schedule |
| **Lights and alerts** | `flash_lights`, `honk_horn` | `button.*_flash_lights`, `button.*_honk_horn`, `switch.*_parking_lights` |
| **Charging** | `close_charge_port`, `open_charge_port`, `set_charge_limit`, `start_charge`, `stop_charge` | `button.*_start_charging`, `button.*_stop_charging`, the two charge-port buttons (absent when **Motorized charge-port controls** is off), `number.*_charge_limit` |
| **Securing actions** | `close_trunk`, `close_windows`, `lock`, `sentinel_on` | `lock.*_door_lock` (locking), `button.*_close_windows` (present but refuses - no verified close command), `button.*_close_trunk`, `switch.*_x_guard` (arming), `number.*_sentinel_sensitivity` |
| **Access actions** | `open_trunk`, `open_windows`, `sentinel_off`, `unlock`, `vent_windows` | `lock.*_door_lock` (unlocking), `button.*_open_trunk`, `button.*_open_windows` and `button.*_vent_windows` (both send the verified command, which vents), `switch.*_x_guard` (disarming) |
| **Accessory power** | `trunk_power_off`, `trunk_power_on` | `switch.*_trunk_power`, `number.*_trunk_power_timer` |
| **Firmware and OTA** | `auto_upgrade_off`, `auto_upgrade_on`, `cancel_ota`, `ota_feedback`, `trigger_ota` | `switch.*_ota_auto_upgrade` |
| **Advanced and raw commands** | `execute_scene`, `send_raw_mqtt`, `toggle_scene` | none — services only |

Groups are split by **what an action does to the car, not by subsystem**, which is why
the door lock, the windows, the trunk and X-Guard each appear twice. Locking, closing
and arming only ever *reduce* access; unlocking, opening and disarming hand the car
over. A "Safe" install can lock itself while a leaked token still cannot open the
doors.

`send_raw_mqtt` and the X-Combo scenes are in **Advanced and raw commands**, because a
raw command can express every other group - `unlock` included - and a scene is an
opaque server-side program. Leaving that group at Full access makes every other setting
advisory.

## Setting it

* **During setup** - the last screen before the config entry is created carries
  **Command permissions** and **Allow the integration to wake the car**.
* **Later** - _Settings -> Devices & services -> XPENG -> **Configure**_. The command
  permissions are the first two fields, pre-filled with what is currently set, so
  widening or narrowing it is one dropdown and a submit. The rest of the form is
  unchanged and already filled in.
* **When the renewal app creates the entry** - the app's own
  [`command_mode`](./app-options#xpeng-credential-renewal) option is what it passes, so
  an app-driven install lands on Safe Plus rather than silently on Full access.

It is **per config entry**, so a two-car install sets each independently, and it takes
effect when the entry reloads - a few seconds, with no restart.

## When a command is refused

The refusal is loud rather than silent. The call raises with the group, the state and
where to change it:

```
'xpeng.unlock' is blocked by this integration's command permissions: the 'Access
actions' group is set to disabled in this integration's command permissions (mode:
safe). Change it in Settings > Devices & Services > XPENG > Configure > Command
permissions.
```

One warning per command goes to the log, and every refusal also fires an
`xpeng_command_denied` event, so an attempt can be alerted on rather than discovered
later:

```yaml
triggers:
  - trigger: event
    event_type: xpeng_command_denied
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: >-
        Blocked {{ trigger.event.data.what }} ({{ trigger.event.data.group_label }},
        mode {{ trigger.event.data.mode }}): {{ trigger.event.data.reason }}
```

The event carries `what` (the service or entity id), `group`, `group_label`, `mode`,
`permission`, `action`, `reason`, and `user_id` when the caller had one.

## Allow wake

Waking a sleeping car costs a little of its 12V battery, so it is a separate toggle
rather than part of any group. With **Allow the integration to wake the car** off, the
integration is strictly passive: it sends queries only, and the sensors are staler for
it - see [The car is not woken up](../guide/overview#the-car-is-not-woken-up). The
internal watchdog honours it too, degrading to a passive query instead of a
force-upload. Charge and drive polls are unaffected, because the car is already awake.

## What this does not protect against

The reassuring version would be a lie:

* It does **not** stop an administrator. Whoever can open **Configure** can change the
  mode, and whoever can read or write your Home Assistant configuration files can
  change it without you.
* It is **not** a per-user permission. It is the install's ceiling. A logged-in user
  must additionally be allowed to control at least one XPENG entity; that part is Home
  Assistant's own groups. Calls with no user at all cannot be checked that way, which
  is exactly why the ceiling applies to them.
* The **account credentials are the same at every level**. The mode limits what this
  integration will *do*, not what the account *could* do.

What it does buy: an assistant or a dashboard that cannot unlock your car, an
automation that cannot fire a raw command by accident, and a token that stops being a
set of car keys.

## Upgrading

| Situation | Result |
| --- | --- |
| An entry that predates the option, never configured | **Full access** - no capability is taken away from an existing automation |
| A new entry (setup screen, or the renewal app creating one) | **Safe Plus** |
| A stored `command_mode` of `charging` | Resolved as `safe_plus` - the old name for the same bundle |
| A group added in a later version | Full, for that group, until someone changes it |

The preset shown is always derived from the stored group values, so the label and the
behaviour cannot drift apart.
