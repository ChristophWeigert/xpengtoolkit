# XPENG integration — events and example automations

The integration fires several Home Assistant events. This directory has
working example automations you can paste into HA's automations UI (or
into `configuration.yaml` under `automation:`) and edit.

## Events fired

All event data comes from `custom_components/xpeng/coordinator.py`.

### `xpeng_ota_available`

Fired when XPENG enrols the VIN in an OTA campaign and the integration
detects the new `upgradeId`. Fires once per `upgradeId` transition —
won't re-ping for the same campaign.

| Key | Type | Notes |
|---|---|---|
| `vin` | str | Vehicle VIN |
| `upgradeId` | int | New campaign's upgrade ID |
| `previousUpgradeId` | int \| null | Last-seen ID, null if first |
| `osVersion` | str | Target XOS version |
| `osName` | str | Target OS name (e.g. "XOS") |
| `releaseNotesUrl` | str \| null | XPENG-hosted release notes URL |
| `estimateCostMinutes` | int \| null | XPENG's ETA for the install |
| `packageSize` | str \| null | Package size string |
| `timestamp` | iso8601 str | When HA detected the transition |

Example: [`automation_ota_available_pushover.yaml`](./automation_ota_available_pushover.yaml).

### `xpeng_xos_version_changed`

Fired when the `xos_version` sensor changes value between two REST poll
cycles (i.e. an XOS update completed and the new version is now
reported by the cloud). The integration also has a **built-in listener**
for this event that auto-writes a snapshot to
`<config>/xpeng_snapshots/`; you don't need to write a user automation
for the snapshot itself.

| Key | Type | Notes |
|---|---|---|
| `vin` | str | Vehicle VIN |
| `old_version` | str | Pre-update XOS version string |
| `new_version` | str | Post-update XOS version string |

Example: [`automation_xos_version_changed_snapshot.yaml`](./automation_xos_version_changed_snapshot.yaml).

### `xpeng_notification`

Fired when XPENG cloud sends a push notification (MQTT `msg_type=3` or
the REST notifications endpoint). The payload is whatever XPENG sent
with the addition of `source` (`"mqtt"` or `"rest"`), `msg_id`,
`timestamp`, and `local_time`. Useful for surfacing sentinel alerts,
charge-complete pings, and door-unlock warnings.

### `xpeng_command_feedback`

Fired when a command sent to the car (climate on, lock, unlock, etc.)
gets a server response. Lets you react to failed commands (`code != 0`)
or specific error codes (`code=12 "Vehicle controls in used"` → fire
`xpeng.clear_stuck_commands`).

| Key | Type | Notes |
|---|---|---|
| `service_type` | int | The MQTT service_type the command used |
| `msg_type` | int | The MQTT msg_type of the response |
| `code` | int | XPENG response code (0=success) |
| `message` | str | XPENG's English message string |
| `msg_id` | str | The MQTT message ID this is a response to |

### `xpeng_mqtt_message` / `xpeng_mqtt_discovery`

Diagnostic-only events. Fire for every MQTT message and every probed
topic respectively. Useful when debugging or extending the integration;
not recommended for production automations (high volume).

## Services worth pairing with events

- `xpeng.capture_snapshot` — write a JSON dump of every coordinator-tracked
  field to `<config>/xpeng_snapshots/`. Auto-fires on
  `xpeng_xos_version_changed`. Manual `reason` field optional.
- `xpeng.clear_stuck_commands` — clear the server-side pending vehicle
  command queue. Pair with `xpeng_command_feedback` where `code=12`.
- `xpeng.check_ota` — manually refresh OTA campaign state. Rarely
  needed since the integration polls every 5 minutes.

## Notify service

Examples use `notify.pushover` because that's the user's setup. Substitute
your own: `notify.mobile_app_<your_device>`, `notify.telegram`, etc. All
the trigger templating remains the same.
