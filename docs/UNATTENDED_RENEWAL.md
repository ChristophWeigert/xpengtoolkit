# Unattended renewal

XPENG retires a session about two hours after the login that created it, and a
refresh cannot extend it (measured: 2h12m, 2h03m, ~2h, on two accounts, with the
refresh loop healthy right up to the moment the server ended it). So an install
that is meant to run for weeks has to *log in again* before that happens — which
normally needs a human to drag a slider.

Two add-ons remove the human, and an automation decides when:

```
automation (schedule)  ->  rest_command  ->  XPENG credential renewal (worker)
                                              |  POST /solve   -> slider solver
                                              |  pwdLogin + guardian -> new pair
                                              `  xpeng.import_credentials (service)
```

The automation is the clock and the policy. The worker holds the account password
and does the privileged part. The integration validates the result before anything
is swapped, exactly as the Repairs card does.

## 1. Install the two add-ons

Both are needed, and both are ordinary add-ons:

* **XPENG slider solver** — `xpengtoolkit/slider-solver`, solves the login captcha.
* **XPENG credential renewal** — this repository. Settings -> Add-ons -> Add-on Store
  -> three-dot menu -> Repositories -> add
  `https://github.com/xpengtoolkit/xpengtoolkit`, then install it.

Configure the worker:

| Option | Meaning |
|---|---|
| `email` / `password` | the account this install renews. A second account, never the one in the app: XPENG keeps one session per account, so the app logging in evicts this one. |
| `device_id` | optional. Leave empty and the first renewal's device id is remembered and reused — a fresh identity every two hours would keep changing what XPENG sees. |
| `solver_url` | defaults to `http://local_xpeng_slider_solver:8080` (the solver add-on, over the internal network). |
| `solver_token` | the solver's `solver_token`, if you set one. |
| `renew_token` | what the automation presents to `POST /renew`. **Set this** — the worker holds your password. |
| `pair_dir` | where the fresh pair is written for Home Assistant to read; `/config/xpeng-secrets` by default. |

Give the pair directory a second thought: it is live credentials on disk, readable
by the Core process. `chmod 700` on the directory, and do not put it in a share.

## 2. Home Assistant side

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
- alias: XPENG - renew credentials before the session expires
  id: xpeng_renew_credentials
  mode: single
  max_exceeded: silent
  trigger:
    # Fires every 10 minutes; the condition is what makes it a ~100 minute cycle.
    - platform: time_pattern
      minutes: "/10"
    # Or immediately, if the current session is already gone.
    - platform: state
      entity_id: binary_sensor.xpeng_<your_car>_credentials_valid
      to: "off"
      for: "00:02:00"
  condition:
    - condition: template
      value_template: >-
        {{ states('input_datetime.xpeng_last_renewal') in ['unknown', 'unavailable']
           or (now().timestamp()
               - state_attr('input_datetime.xpeng_last_renewal', 'timestamp')) > 6000 }}
  action:
    - service: rest_command.xpeng_renew
      response_variable: renew
      continue_on_error: true
    - if:
        - condition: template
          value_template: "{{ renew is defined and renew.status == 200 }}"
      then:
        - service: input_datetime.set_datetime
          target:
            entity_id: input_datetime.xpeng_last_renewal
          data:
            timestamp: "{{ now().timestamp() }}"
      else:
        - service: persistent_notification.create
          data:
            title: XPENG renewal failed
            message: >-
              {{ renew.content if renew is defined else 'the worker did not answer' }}
```

### Why a helper is the clock

`credentials_valid` carries `age_s`, but that is the time since the last *mint*
(about 45 seconds while healthy), not the age of the session — useless for this.
What matters is when the last **login** happened, and a renewal *is* a login, so
"last successful renewal" is exactly the right clock. Stamping `input_datetime` on
success gets that for free, and it stays correct even if XPENG moves the cap.

6000 seconds is 100 minutes: 20 minutes inside the measured ~2 hours, which leaves
room for a retry if something fails. The automation re-fires every 10 minutes while
the condition holds, so a failure retries on its own.

### Versions

`response_variable` on `rest_command` needs Home Assistant 2023.7 or newer. On
anything older, drop it and the `if`/`then`: the worker already raises its own
persistent notification after two consecutive failures, so failures stay visible.

## 3. Verifying it

`GET http://<your-ha-host>:8090/healthz` reports renewals, failures, consecutive
failures and the last outcome — including what `import_credentials` returned.

To test without waiting two hours, set the comparison in the condition to a small
number (e.g. 300) for a few cycles, confirm `credentials_valid` comes back healthy
with a fresh age, then put 6000 back.
