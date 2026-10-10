# Unattended renewal

XPENG retires a session about two hours after the login that created it, and a
refresh cannot extend it (measured: 2h12m, 2h03m, ~2h, on two accounts, with the
refresh loop healthy right up to the moment the server ended it). So an install
that is meant to run for weeks has to *log in again* before that happens — which
normally needs a human to drag a slider.

Two apps remove the human, and an automation decides when:

```
automation (schedule)  ->  rest_command  ->  XPENG credential renewal (worker)
                                              |  POST /solve   -> slider solver
                                              |  pwdLogin + guardian -> new pair
                                              `  xpeng.import_credentials (service)
```

The automation is the clock and the policy. The worker holds the account password
and does the privileged part. The integration validates the result before anything
is swapped, exactly as the Repairs card does.

> Home Assistant renamed add-ons to **apps** in 2026.2. The store, the options and
> everything below are the same thing under the new name.

## 1. Install the two apps

Both are needed, and both are ordinary apps:

* **XPENG slider solver** — `xpengtoolkit/slider-solver`, solves the login captcha.
* **XPENG credential renewal** — this repository. Settings -> Apps -> Install app
  -> three-dot menu -> Repositories -> add
  `https://github.com/xpengtoolkit/xpengtoolkit`, then install it.

Configure the worker:

| Option | Meaning |
|---|---|
| `email` / `password` | the account this install renews. A second account, never the one in the app: XPENG keeps one session per account, so the app logging in evicts this one. |
| `device_id` | optional, and worth pinning — see [the device identity](#the-device-identity) below. Leave empty and the first renewal's device id is remembered and reused, so the identity at least does not change every two hours. |
| `solver_url` | where the solver app listens. Defaults to `http://571f4c18-xpeng-slider-solver:8080`, the solver's container hostname as installed from the store. |
| `solver_token` | the solver's `solver_token`, if you set one. |
| `renew_token` | what the automation presents to `POST /renew`. **Set this** — the worker holds your password. |
| `pair_dir` | where the fresh pair is written for Home Assistant to read; `/config/xpeng-secrets` by default. |

Give the pair directory a second thought: it is live credentials on disk, readable
by the Core process. `chmod 700` on the directory, and do not put it in a share.

### If `solver_url` does not resolve

This is the one setting that depends on how the solver was installed, and getting
it wrong looks like "the slider never gets solved":

| How the solver was installed | `solver_url` |
|---|---|
| from the store, `xpengtoolkit/slider-solver` | `http://571f4c18-xpeng-slider-solver:8080` (the default) |
| as a local app folder named `slider_solver` | `http://local-xpeng-slider-solver:8080` |
| anything else | `http://<your-ha-host-ip>:8080`, e.g. `http://192.168.1.10:8080` |

**It is the hostname that resolves, not the slug** — hyphens, not underscores.
The slug forms (`571f4c18_xpeng_slider_solver`,
`local_xpeng_slider_solver`) fail with `No address associated with hostname`;
those were the names people were told to use, and the shipped default, which is
why this is worth stating plainly. The plain LAN address always works, so it is
the safe answer when in doubt. Changing this option restarts the app.

## 2. First run: there is no config entry yet

A renewal *imports* the pair it minted, and importing needs somewhere to import
it to. On a brand-new install there is no XPENG entry yet, so the first renewal
gets all the way through the login and then fails at the last step with

```
import_credentials returned 400: No XPENG config entry to import credentials into
```

**That is expected, and it is not a failure of the renewal.** The pair is already
written to `pair_dir` at that point. Finish the bootstrap once by hand:

1. Check the pair exists:
   `ls -l /config/xpeng-secrets/` → `xpeng_tokens.json` and `xpeng_tenant.json`.
2. Settings -> Devices & Services -> **Add integration** -> *Xpengtk*.
3. VIN: your car's VIN. Authentication method: **Import tokens from the
   onboarding tool (recommended)**.
4. Give the two paths below `pair_dir`:
   `/config/xpeng-secrets/xpeng_tokens.json` and
   `/config/xpeng-secrets/xpeng_tenant.json`.

From then on renewals complete on their own, and `import_credentials` swaps the
credentials in place without a restart.

The app itself needs permission to call that service; it asks for
`homeassistant_api` in its own config, which is what makes the import (and its
failure notifications) possible. If you ever see `401 Unauthorized` from the
import step rather than a 400, that grant is what is missing.

## 3. Home Assistant side

`configuration.yaml`:

```yaml
rest_command:
  xpeng_renew:
    url: http://<your-ha-host>:8090/renew
    method: POST
    headers:
      Authorization: !secret xpeng_renew_auth     # "Bearer <renew_token>"
    timeout: 300

input_datetime:
  xpeng_last_renewal:
    name: XPENG last renewal
    has_date: true
    has_time: true
```

Then the automation. Two triggers on purpose: a schedule that stays ahead of the
cap, and the failure state for the case where it did not.

```yaml
- alias: XPENG renew credentials
  id: xpeng_renew_credentials
  mode: single
  max_exceeded: silent
  triggers:
    # Fires every 10 minutes; the interval condition is what makes it ~100 minutes.
    - trigger: time_pattern
      minutes: "/10"
      id: periodic
    # Or immediately, if the current session is already gone.
    - trigger: state
      entity_id: binary_sensor.xpeng_<your_car>_credentials_valid
      to: "off"
      for: "00:02:00"
      id: invalid
  conditions: []
  actions:
    # The interval is checked INSIDE the action, not as an automation condition,
    # so that the already-broken case is not made to wait it out: while the
    # credentials are invalid the car is unreachable, and gating that path would
    # leave it that way for up to a whole interval.
    - if:
        - condition: or
          conditions:
            - condition: state
              entity_id: binary_sensor.xpeng_<your_car>_credentials_valid
              state: "off"
              for: "00:02:00"
            - condition: template
              value_template: >-
                {{ states('input_datetime.xpeng_last_renewal')
                   in ['unknown', 'unavailable']
                   or (now().timestamp()
                       - state_attr('input_datetime.xpeng_last_renewal', 'timestamp')) > 6000 }}
      then:
        # Order is the failure policy. A non-2xx answer (the worker returns 502
        # for a failed renewal, 401 for a bad token) raises, an action error
        # stops the sequence, and the stamp below is therefore skipped — so a
        # failed renewal is retried on the next trigger instead of being
        # suppressed for a whole interval.
        - action: rest_command.xpeng_renew
        - action: input_datetime.set_datetime
          target:
            entity_id: input_datetime.xpeng_last_renewal
          data:
            timestamp: "{{ now().timestamp() }}"
```

That order is not cosmetic and it is worth keeping. Verified on a live install:
with the worker answering `401` the helper kept its previous value, and with a
`200` it was stamped.

### Why a helper is the clock

`credentials_valid` carries `age_s`, but that is the time since the last *mint*
(about 45 seconds while healthy), not the age of the session — useless for this.
What matters is when the last **login** happened, and a renewal *is* a login, so
"last successful renewal" is exactly the right clock. Stamping `input_datetime` on
success gets that for free, and it stays correct even if XPENG moves the cap.

6000 seconds is 100 minutes: 20 minutes inside the measured ~2 hours, which leaves
room for a retry if something fails. The automation re-fires every 10 minutes while
the condition holds, so a failure retries on its own.

## 4. The device identity

The app logs in as a *device*, and which device id that is turns out to matter more
than the schedule. Measured on 2026-10-10 on one install:

* with a freshly minted random device id, `credentials_valid` went invalid every
  ~2h07m–2h27m — the cap;
* after re-onboarding the same account with the device id already used by the
  phone, the session stayed valid for **3h44m and counting**, with no `12010202`
  ("Login status has expired") anywhere in the log.

So a session bound to an identity XPENG has seen before outlives one bound to a
brand-new identity. Practical consequences:

* The 100-minute cycle is correct either way, and it is what makes a fresh install
  work: a new install mints a new id and gets ~2 hours.
* If you want the longer sessions, set `device_id` explicitly to an id that
  install has used before (32 lowercase hex characters) and leave it set.
* Whatever you do, do not let it change on every renewal — that is what the app's
  remembered id in `/data/device_id.txt` prevents.

## 5. Seed the account in the app first

Symptoms: renewal works, credentials are valid, the car is plainly bound to the
account (the API lists it), and yet **most entities sit at `unknown`** — often 90
of ~124 — with a few odd ones populated. The log repeats:

```
XPENG MQTT connection refused: rc=Not authorized (rc_str=Not authorized)
sensor.<car>_mqtt_listener      = No messages
sensor.<car>_last_data_update   = No data
sensor.<car>_polling_status     = Reconnecting
```

The full vehicle state arrives over **MQTT**; the REST path alone yields a partial
snapshot, so without MQTT most entities never get a value. The broker is not
rejecting bad credentials — it is refusing to authorize an account that has only
ever logged in *headlessly*.

**Fix: sign the account into the official XPENG app once.** It is authorized
immediately afterwards. Measured on a fresh test account, within a minute of the
app login:

| | before | after |
|---|---|---|
| entities unknown | 97 / 124 | 19 / 124 |
| `last_data_update` | `No data` | fresh |
| `polling_status` | `Reconnecting` | `Parked` |

This is not a user or session limit. Two freed MQTT sessions (another Home
Assistant for the same car switched off entirely, and a phone signed out) changed
nothing; the refusal only stopped when the account was seeded. It is also not the
device id — the refusal persisted while the install presented the phone's own id,
so it is the *account's* registration state, not the id string. A single refusal
can still appear now and then after seeding, as the credential rotation races; it
recovers on its own and `last_data_update` keeps advancing.

### What the app login actually does — and the session collision

Measured on a fresh account, three readings:

| | before the app login | right after it | ~4 min later, **app still signed in** |
|---|---|---|---|
| entities unknown | 97 / 124 | 19 / 124 | 97 / 124 |
| `last_data_update` | `No data` | live | `No data` |
| `polling_status` | `Reconnecting` | `Parked` | `Reconnecting` |
| `Not authorized` | ongoing | stopped | resumed |

The app login authorises the account and data flows **immediately** — and then it
stops again within minutes, *while the app is still signed in*. The likely reason
is the one-session-per-account rule: the app's login evicts the install's session,
so the install's MQTT credentials die and the refusals resume. The phone effectively
takes the session.

So "sign the app in once" is **not** a stable fix, and it is not simply a one-off
seeding step. An earlier revision of this section blamed the app being *signed out*;
that was wrong — the regression was observed before the sign-out happened.

**Measured: a headless renewal does not restore it.** With the app signed out, a
complete headless renewal — fresh login, new pair minted and imported,
`createMqttAccount` OK (`[+] LOGIN OK uid=10225403`) — left the install exactly
where it was:

```
entities unknown = 97 / 124
mqtt_listener    = No messages
last_data_update = No data
Not authorized   = still arriving
```

So the two measurements together are:

| state | MQTT | vehicle data |
|---|---|---|
| app signed in | authorised | flows immediately, then stops within minutes |
| app signed out + fresh headless renewal | refused | none |

Live vehicle data and unattended headless renewal are therefore in **tension** on
one account: the app's presence is what gets the broker to authorise the account,
and the app and the install evict each other under the one-session-per-account
rule. On a dedicated second account, expect renewal to work and data not to — plan
for it rather than debugging it.

The reference to compare against is the production install that *does* show live
data: same integration, same car, zero `Not authorized` events. Its account
presumably has the app signed in and no competing headless login, which is worth
confirming before concluding anything further.

## 6. When it goes wrong

Read these in order:

1. **`/config/xpeng-renew-last-run.log`** — the complete output of the last
   renewal, written by the app on every run. It contains the tool's own progress
   lines and whatever the browser or the API said, so it is usually the whole
   answer. Treat it as sensitive: it describes the account's login attempts.
2. **`GET http://<your-ha-host>:8090/healthz`** — renewals, failures, consecutive
   failures, and the last outcome including what `import_credentials` returned.
3. **The HA log**, for `homeassistant.components.rest_command` errors: they name
   the status the worker answered with.
4. **Most entities `unknown` while the credentials are healthy** — that is the
   MQTT authorization case in [section 5](#5-seed-the-account-in-the-app-first),
   not a credential problem. Look for `MQTT connection refused: rc=Not authorized`.

The worker raises a persistent notification after **two consecutive failures**.
Note that Home Assistant 2026.10 no longer exposes persistent notifications as
`persistent_notification.*` entities, so you will not find them with a template or
in the state list — look in the notifications panel in the UI.

## 7. Testing it without waiting two hours

Open the interval on purpose rather than shortening the code: set
`input_datetime.xpeng_last_renewal` to a time more than 6000 seconds ago (Developer
Tools -> Actions -> `input_datetime.set_datetime`), then run the automation from
Settings -> Automations. A successful cycle stamps the helper back to now; a failed
one leaves it alone, which is the behaviour you want to confirm.

Because the interval is checked inside the action, "Run" in the UI works for this.
An earlier revision gated on the trigger id instead, which silently did nothing
when run by hand — worth knowing if you copy the YAML from somewhere older.
