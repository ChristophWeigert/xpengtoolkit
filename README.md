# Xpengtk

A Home Assistant custom integration for XPENG vehicles (`domain: xpeng`).

Unofficial, reverse-engineered, and not affiliated with XPENG.

## Unattended renewal

XPENG retires a session about two hours after the login that created it, and a
refresh cannot extend it - the refresh loop keeps working right up to the moment
the server ends the session. Two apps take the human out of the loop:

* **[XPENG slider solver](https://github.com/xpengtoolkit/slider-solver)** - solves
  the login captcha. That slider is the only human step in onboarding, so this is
  what makes an automated login possible at all.
* **XPENG credential renewal** - this repository. It mints a fresh pair with the
  toolkit's own login code and imports it through the integration's
  `xpeng.import_credentials` service, which validates the pair by minting MQTT
  credentials, exactly as the Repairs card does.

[Steps 4-5](#step-4---install-the-two-apps) install the apps and mint the first
pair with the solver, and [step 6](#step-6---add-the-renewal-automation) adds the
automation that calls them. Three things are easy to miss:

- **The first renewal is expected to fail** when the integration has no config
  entry yet: it mints the pair and then has nowhere to import it. The pair has
  already landed in the directory at that point, and the entry is created once,
  by hand, with **Import tokens**.
- **Set `solver_url` if the solver was not installed from the store.** The default
  names the store install's container hostname
  (`http://571f4c18-xpeng-slider-solver:8080`). A local-folder install is
  `http://local-xpeng-slider-solver:8080`, and the Home Assistant host's own
  address always works. The underscore slug form (`local_xpeng_slider_solver`)
  does **not** resolve - hyphens do.
- **Keep one `device_id`.** Set it once and leave it; changing it on a live
  install is the stale-client trap in [Troubleshooting](#troubleshooting).

**Both apps hold credentials** - set their tokens, and keep the pair directory out
of any share.

The service is useful by hand too: a credential swap no longer has to wait for the
Repairs card to appear first (it is raised only after the broker refuses, or on the
hourly poll - up to an hour late).

```yaml
service: xpeng.import_credentials
data:
  tokens_path: /config/xpeng-secrets/xpeng_tokens.json
  tenant_path: /config/xpeng-secrets/xpeng_tenant.json
```

## The whole process at a glance

### What "identity" means here - three separate things

This is the part that trips people up, so it is worth being explicit. Nothing
about your car's identity changes; what gets assembled is *this install's* set of
credentials. Three independent pieces, from three different places:

| Piece | What it is | Where it comes from | Who does it |
|---|---|---|---|
| **1. App mTLS certificate + key** | `CN=xp_app_client01`, the same for every XPENG user. Proves *the app*, not you. | Extracted from an XPENG APK | You, once (step 2) |
| **2. Device identity** | A random 32-hex id unique to this install, bound into the signed requests and the MQTT client id | **Minted automatically** by the onboarding tool when it fetches the tenant | Nobody - it is handed to you |
| **3. Account tokens** | Your account's access/refresh token, and the tenant key pool they are sealed with | Minted once: by the renewal app (step 5), or by the onboarding tool on a desktop ([alternative](#alternative-mint-the-first-pair-yourself)) |

You never type a device id anywhere. The tool generates it, and the integration
**adopts** it from the imported tenant rather than minting a new one - that
binding is exactly why the token files and the tenant must be imported together.

**Do not sign the official app in on this account.** A shared car needs no app
visit first - the integration works fully headless - and signing in makes the two
evict each other, because XPENG allows one session per account (see
[Use a dedicated account](#use-a-dedicated-account---do-not-use-your-primary-account)).
If the entities come up empty, that is the stale-client case in
[Troubleshooting](#troubleshooting), not a missing app login.

### The steps

**Before step 1, once:** create a dedicated XPENG account and have your main
account share the car to it (see
[below](#use-a-dedicated-account---do-not-use-your-primary-account)). Accept the
share from the new account if the app asks you to - but **do not leave the
official app signed in as that account**, for the reason given just above.

| # | Step | Where | Roughly how long |
|---|---|---|---|
| **1** | [Install the integration](#installation) - add the HACS custom repository, install, restart HA | Home Assistant | 2 min |
| **2** | [Download the repository ZIP](https://github.com/xpengtoolkit/xpengtoolkit) and [extract the certificates](#step-2---extract-the-certificates-outside-home-assistant) from an XPENG APK **3.7.0-3.12.0** | Any computer | 5 min |
| **3** | [Copy the three PEM files onto the Home Assistant host](#step-3---copy-the-certificates-onto-the-home-assistant-host) | Home Assistant | 2 min |
| **4** | [Install the two apps](#step-4---install-the-two-apps) (slider solver + credential renewal) and set their tokens | Home Assistant | 5 min |
| **5** | [Run the first renewal](#step-5---run-the-first-renewal) - it logs in, and creates the integration entry itself | Home Assistant | 2 min |
| **6** | [Add the renewal automation](#step-6---add-the-renewal-automation) | Home Assistant | 5 min |

**Only step 2 happens outside Home Assistant**, because extracting the certificates
needs the APK. Nothing else does - including the captcha-gated login, which the
solver app clears.

**Steps 4-5 are what make the install self-renewing**, and step 6 is what makes it
repeat: the automation calls the app, so without it you would be triggering renewals
yourself. Without 4-5 it still works, but only until XPENG retires the session about
two hours later, and then a fresh pair has to be pasted in by hand. If you would
rather not run the apps at all, or
not hand them the account password before you have seen the integration work,
[the alternative](#alternative-mint-the-first-pair-yourself) mints the
pair on a desktop instead and replaces steps 4-5 - it needs Chrome, Node and one
manual slider drag.

Before you start, two things that catch people out:

- **Use a dedicated XPENG account, never your primary one** - see
  [below](#use-a-dedicated-account---do-not-use-your-primary-account).
- **Download an XPENG APK 3.12.0 or older on purpose.** From 3.13.0 onward the
  certificate is no longer extractable from the package.

## Before you install - read this first

**You can get the mTLS client key from the Android APK, offline, with no
jailbreak, no Frida and no phone hacking at all.** You need an XPENG APK build
between **3.7.0 and 3.12.0**, and one command produces all three files - see
[step 2](#step-2---extract-the-certificates-outside-home-assistant).

Why the key matters, and what each channel needs:

| Channel | Needs the client key? | What it can do |
|---|---|---|
| MQTT broker (`iot-app-global`) | **Yes** | Real-time telemetry and vehicle control |
| App REST API (`apps-global`) | **Yes** | Account login, mints the MQTT credentials |
| Web SSO (`iot-web-eur`) | No | **At most** energy-report/dashboard data |

**One hard limit:** this APK route works for **3.7.0 through 3.12.0**. From
**3.13.0** onward XPENG ships an encrypted keystore with the identity supplied as
runtime configuration, and the APK alone is no longer enough. The break is
exactly between 3.12.0 and 3.13.0 - so download an older build on purpose.

The client certificate and key are **not** distributed with this project; you
extract them from your own copy of the official app. See
[`custom_components/xpeng/certs/README.md`](custom_components/xpeng/certs/README.md)
for the full detail, including the version boundary that decides which APK build
you need.

If you cannot obtain the key, this integration is not usable - but for most
people the APK route above now makes that a non-issue.

## Status

Early. It authenticates with your own XPENG account, subscribes to the vehicle
telemetry stream over mutual-TLS MQTT, and exposes sensors, binary sensors and
controls in Home Assistant.

## How it works

### Two channels, one account

The integration talks to XPENG's cloud over two independent channels, and they
do different jobs:

- **MQTT over mutual TLS** (`iot-app-global.xiaopeng.com:8885`) - the live
  vehicle channel. It carries telemetry pushes *and* remote commands. This is
  where the client certificate you supplied is actually used.
- **HTTPS REST** (`apps-global.xiaopeng.com`, mutual TLS too) - the control
  plane. It performs account login, and it mints the short-lived MQTT password
  on a timer.

Both are signed with XPENG's app-tier scheme, and both need the client key.

### Data arrives by push, not by polling

There is no `update()` loop. A single long-lived MQTT connection is opened at
startup, and the broker pushes vehicle state onto it. A passive coordinator
receives those frames on the client's network thread and hands them to Home
Assistant, which notifies entities that the data changed.

That is why entities update the instant the car reports something, and why they
hold their last known value when it doesn't.

### The car does not get woken up

How often the integration *asks* the car for a fresh frame depends on what the
car is doing:

| Car state | What the integration sends |
|---|---|
| Charging, or driving | A force-upload request every `scan_interval` (default 60s). The car is already awake, so this is not a wake-up - and it is necessary, because the cloud's push fan-out stalls often enough to matter. |
| Parked and idle | One *passive* query per `parked_scan_interval` (default 1 hour). A sleeping car answers that with a small stub without waking. |

A parked car is **never** force-woken on a schedule. The only force-upload sent
to a possibly-sleeping car is a single one on a genuine Home Assistant restart,
to seed the entity values.

### Stuck sessions get unstuck

XPENG's cloud push fan-out sometimes goes quiet while the TCP connection still
looks perfectly healthy - server-side, not something the client can prevent.
A silence watchdog notices 300s of no vehicle-state traffic, drops and
re-establishes the connection, and sends a passive query. Reconnects are
serialised behind a lock, because concurrent reconnects produce two clients
sharing one client ID that evict each other from the broker - and setting an
entry up again now stops any client a previous setup left behind, since two
clients under one account is the failure described in
[Troubleshooting](#troubleshooting).

### Credentials rotate, and the identity is per-install

The MQTT password is short-lived and is re-minted from your login tokens every
`mqtt_refresh_interval` (default 45s) - independent of the car's state.

Each installation also mints its **own device identity** on first run, and that
identity is signed into the requests and embedded in the MQTT client ID. The
credentials XPENG issues are bound to it, so **never copy a credential set
between installations** - see the note in Setup.

If the login token has aged out, the refresh recognises XPENG's
`12010101`/`12010104` "Login status expired" response, renews the token, and
retries. If the *refresh* token is dead too, it escalates to reauthentication
rather than retrying forever.

### What you get

Roughly 118 entities across sensors, binary sensors, device tracker, lock,
climate, select, number, switch, button, calendar and image - battery and range,
charging state and charge limits, doors/windows/tyres, location, climate and
seat heating, sentinel/X-Guard, OTA state, and a set of remote commands.

### Notifications

XPENG's notifications do **not** arrive over MQTT and are not pushed to this
integration. They live server-side in the app's **Message Centre**, and this
integration polls that list. A new one appears within
`NOTIFICATION_POLL_INTERVAL` (5 minutes) as:

- **`sensor.<vehicle>_last_notification`** - state is the message text, with
  `count`, `title`, `category`, `msg_type`, `timestamp`, `read` and `msg_id`
  in the attributes
- **`xpeng_notification`** - a bus event carrying the same flattened fields

The Message Centre groups messages into categories, and the API returns **one
category per request**. All six are polled, and every message is tagged with the
category it came from (`category` / `classification` attributes), so a single
sensor covers everything:

| Category | Code | Typically holds |
|---|---|---|
| Vehicle Notification | `XPMOBILE-VEH` | X-Combo, sentry, window/door events |
| XPENG Notification | `XPMOBILE-PUS` | Account and system messages |
| Charging Notification | `XPMOBILE-CHA` | Charge start/stop, completion |
| Service Notification | `XPMOBILE-SER` | Servicing and maintenance |
| Order Notification | `XPMOBILE-ORD` | Orders and purchases |
| Event Notification | `XPMOBILE-JOY` | Campaigns and events |

In practice **Vehicle Notification** is the busy one, and the rest are usually
empty - but an empty category and an unqueried category look identical over the
API (both return `[]`), so all six are queried rather than assumed.

**Triggering automations.** Either form works:

```yaml
automation:
  - alias: X-Combo notification
    triggers:
      - trigger: event
        event_type: xpeng_notification
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.category == 'XPMOBILE-VEH' }}"
    actions:
      - action: notify.persistent_notification
        data:
          message: "{{ trigger.event.data.message }}"
```

or trigger on the sensor's state directly, which is easier to build in the UI.

Two behaviours worth knowing before you rely on this:

- **Only new notifications fire.** The first poll after a restart records the
  existing inbox as a baseline *without* firing, so a restart does not replay
  the whole Message Centre into your automations.
- **Anything that arrives while Home Assistant is down is absorbed into that
  baseline** and will not trigger. If you need those, they are still in
  `recent` and the `count` attribute after startup.

## Requirements

- Home Assistant 2024.6 or newer
- A **dedicated** XPENG account - see below
- The integration's **mTLS certificates**, which you extract yourself - see
  [`custom_components/xpeng/certs/README.md`](custom_components/xpeng/certs/README.md).
  They are deliberately **not** shipped here.

## Use a dedicated account - do not use your primary account

**Create a new XPENG account and share your car to it. Do not sign this
integration in with the account you use on your phone.**

XPENG allows **one active session per account**. This integration signs in as a
second client, so it and the official app fight over that session: your phone app
gets logged out over and over, and the integration gets logged out whenever you
open the app. That is not a bug that can be fixed here - it is how the account
model behaves.

So, once:

1. Create a **new XPENG account** (a second email address works fine).
2. In the **official app**, signed in as your main account, **share the car** to
   that new account (Vehicle -> sharing / "share to another user"; the wording
   varies by region).
3. Accept the share from the new account.
4. Sign this integration in with the **new** account only.

The new account needs no phone number and is never used anywhere else. Your main
account stays logged in on your phone, and this integration keeps its own
session. If you have already pointed the integration at your primary account,
remove the entry and add it again with the dedicated one.

## Installation

This is step 1 of [the process above](#the-steps). The other five steps are
under [Setup](#setup).

### HACS (custom repository)

1. HACS -> Integrations -> three-dot menu -> **Custom repositories**
2. Add `https://github.com/xpengtoolkit/xpengtoolkit` as an **Integration**
3. Install **Xpengtk**, then restart Home Assistant

### Manual

Copy `custom_components/xpeng/` into your Home Assistant `config/custom_components/`
directory and restart.

Then: **Settings -> Devices & Services -> Add Integration -> Xpengtk**.

## Setup

This section covers **steps 2 to 6** of the summary above; step 1 is
[Installation](#installation).

**Sign in with your dedicated account, never your primary one** (see above).

The **only** step that happens outside Home Assistant is extracting the
certificates, because that needs the APK. Everything else, including the login
that XPENG gates with its slider captcha, is done here - the solver app clears
that challenge, which is why the app route needs no desktop.

### Step 2 - extract the certificates (outside Home Assistant)

First get the repository onto the machine you are doing this on. On the GitHub
page, **Code -> Download ZIP**, then unzip it. Do not download single files out
of it - the tools import code from the integration, so the layout has to stay
intact:

```
xpengtoolkit-main/          <- GitHub names the unzipped folder like this
├── custom_components/xpeng/   the integration (HACS installs this)
└── tools/                     the scripts you run in steps 2 and 3
```

**Run every command below from that top folder.** The commonest mistake is
downloading `tools/` on its own - it will not run, and it is what makes the
`tools/...` versus `custom_components/...` paths confusing.

Then one command against an XPENG APK build **3.7.0-3.12.0** gives you the three
PEM files:

```bash
pip install pyjks cryptography
python3 tools/xpeng_apk_certs.py --apk XPENG_3.12.0_APKPure.xapk --out ./certs
```

That writes `ca-chain.crt`, `client-cert-0.pem` and `client-key.pem`, and checks
that the key matches the certificate before declaring success. Exit code `2`
means it found the certificate but could not recover the key - which is what a
3.13.0-or-newer package looks like, so use an older build.

Full detail: [`custom_components/xpeng/certs/README.md`](custom_components/xpeng/certs/README.md)
which also carries the extraction steps for each platform.
On Windows, [`docs/WINDOWS_WSL_SETUP.md`](docs/WINDOWS_WSL_SETUP.md) walks this
whole process through WSL.

### Step 3 - copy the certificates onto the Home Assistant host

Step 2 ran on your own computer; the integration runs on Home Assistant, so the
three files have to get there. Put them **outside** the integration folder:

```
/config/xpeng-certs/
├── ca-chain.crt
├── client-cert-0.pem
└── client-key.pem
```

`/config/xpeng-certs` is what the rest of this README assumes. Any path works
except `custom_components/xpeng/certs/`: that is the default, but it sits inside
the integration, which an update replaces - your keys would go with it.

Getting them there is up to you and your setup: the **Samba share**, the **File
editor** app, `scp`, or anything else that can write to `/config`. Then check they
arrived:

```bash
ls -l /config/xpeng-certs      # three files
```

These are live credentials, so set the directory to `700` and the files to `600`,
and do not leave the folder exposed in a share.

**Nothing needs configuring for either location.** The integration looks for
`ca-chain.crt` in `/config/xpeng-certs`, then in its own `certs/` directory, and
uses the first one that has it. Only if you keep the three files somewhere else do
you need to say so: *Settings -> Devices & Services -> XPENG -> Configure ->
**Certificate directory***. That form validates the directory, and refuses to save
unless all three files are in it.


### Alternative: mint the first pair yourself

Take this route if you would rather not hand the account password to an app before you
have seen the integration work - or if you cannot run the solver. It needs a desktop
machine (Mac/Linux/Windows with Node and a Chrome install), and it replaces **steps
4-5** with the two below. Skip step 6 as well: with no app installed there is nothing
for that automation to call, so renewals are yours to trigger until you come back and
do step 4.

Run this from the top folder of the repository you unzipped in step 2. It opens a
browser window; **drag the slider once**. It then writes two files:

```bash
python3 tools/onboard_email_login.py \
    --email YOUR_SECONDARY_EMAIL --password 'YOUR_PASSWORD' --save --mqtt
```

It writes **`xpeng_tokens.json`** and **`xpeng_tenant.json`** into
`custom_components/xpeng/`. Note the plural - `xpeng_tokens.json`.

See [`tools/README.md`](tools/README.md) for dependencies and options.

#### Skipping the drag entirely

That slider gesture is the one human step, and it is also the reason an expired
session cannot renew itself. A hosted solver removes it:

```bash
python3 tools/onboard_email_login.py \
    --email YOUR_SECONDARY_EMAIL --password 'YOUR_PASSWORD' \
    --captcha-solver http://your-solver-host:8080 --save --mqtt
```

With `--captcha-solver` no browser opens here: the tool asks the service to clear
the slider and gets the same `validate` a human drag would produce. Everything
after that - the `drag/validate` POST, the guardian token, the `pwdLogin` retry -
is unchanged. Only `guardian_token.mjs` is still needed locally, so a caller that
only has the solver can run without Playwright or Chrome at all.

The service is self-hosted and separate: see
[xpengtoolkit/slider-solver](https://github.com/xpengtoolkit/slider-solver)
(`docker run -e SOLVER_TOKEN=... -p 8080:8080`). It solves the account owner's own
challenge on a host they control - keep it private, and never expose it without
`SOLVER_TOKEN`.

That is what makes renewal unattended: with the whole flow headless, an install
can mint fresh tokens before its session ends rather than waiting for the Repairs
card. Sessions currently last about two hours from login (measured), so a renewal
scheduled comfortably inside that window keeps an install alive indefinitely.

Two things happen here that people do not expect:

- **This is where your device identity is created.** The tool mints a random
  32-hex id for this install and stores it in the tenant. You do not choose it,
  type it, or need to know it - but it is why the two files belong together.
- The tool also stores the **tenant**: the key pool your tokens are sealed with.
  It is not reproducible, so it must be imported alongside the tokens.

Why this step exists, and cannot be folded into Home Assistant: the only
human-only part of XPENG's login is that slider gesture. Everything else is
plain HTTP.

#### Create the entry from those files

**Settings -> Devices & Services -> Add Integration -> Xpengtk**, then choose
**Import tokens** and paste the contents of both files: open each one in a text editor
and copy the whole thing, JSON wrapper and all. The paths are read inside the Home
Assistant container, so they cannot see a file sitting on your desktop - pasting is what
works here.

No captcha, no password, and it works for accounts that are captcha-gated. The
integration **adopts the device id from the imported tenant** rather than minting a new
one, so if you ever see it mint a fresh id instead, the tenant did not come through and
the credentials will be rejected.

### Step 4 - install the two apps

This is the part that makes renewal unattended, and it is where the two companion
apps come in:

* **[XPENG slider solver](https://github.com/xpengtoolkit/slider-solver)** - clears
  the login captcha, which is the one human step in onboarding.
* **XPENG credential renewal** - this repository. It mints a fresh pair ahead of the
  two-hour cap and imports it with `xpeng.import_credentials`, the same service the
  Repairs card uses.

**Settings -> Apps -> Install app -> three-dot menu -> Repositories**, add this
repository's URL and the solver's, then install both. (Home Assistant renamed
add-ons to **apps** in 2026.2; older releases call the same screen the Add-on
Store.)

Then configure the renewal app:

| Option | Value |
|---|---|
| `email` / `password` | the dedicated account - never the one signed into the phone app |
| `renew_token` | any secret. **Set it** - the worker holds your password, and this is what the automation presents |
| `solver_url` | the default if the solver came from the store; otherwise see the [solver_url table](docs/UNATTENDED_RENEWAL.md#if-solver_url-does-not-resolve) |
| `solver_token` | the solver's token, if you set one |
| `device_id` | leave blank and the first renewal pins it, or set it yourself. Do not change it afterwards - [it is the stale-client trap](#most-entities-are-unknown) |
| `pair_dir` | `/config/xpeng-secrets` by default. `chmod 700` it |

The pair directory is live credentials on disk: `chmod 700`, and keep it out of any
share.

### Step 5 - run the first renewal

This is the whole install in one call: the app logs in, the solver clears the slider,
it writes the pair to `pair_dir` (`/config/xpeng-secrets` by default), and - because
there is still no config entry - it creates that entry itself out of the same pair.
Nothing is pasted, and nothing is typed.

```bash
curl -X POST -H "Authorization: Bearer <renew_token>" http://<your-ha-host>:8090/renew
```

The integration then appears under **Settings -> Devices & Services** as
`XPENG <last six of your VIN>`, and the entities fill in. The response says
`"bootstrapped"` with the VIN it created the entry from.

If it instead reports that it has no VIN, the token file predates the onboarding tool
recording one: either set this app's **`vin`** option to your 17-character VIN, or add
the integration by hand once, as
[the alternative](#alternative-mint-the-first-pair-yourself) describes.

**Why the app does this and not the integration.** `xpeng.import_credentials` cannot
create the entry, which is worth knowing before "fixing" it there: on an install with
no entry that service does not exist yet, because the integration's component-level
setup only runs once an entry exists. The config flow *is* loaded on demand, so that is
what the app drives - and creating the entry is what registers the service for every
renewal after this one. The app logs what it did to
`/config/xpeng-renew-last-run.log`.

### Step 6 - add the renewal automation

The app only acts when it is called, so something has to call it: ahead of the cap
on a schedule, and immediately when the credentials are already invalid. That takes
a `rest_command` pointing at the app's `/renew`, a helper to record when it last
ran, and an automation joining them.

Copy all three from
[`docs/UNATTENDED_RENEWAL.md`](docs/UNATTENDED_RENEWAL.md#3-home-assistant-side),
which also explains why the interval is checked inside the action rather than as a
condition, and how to test the whole thing without waiting two hours for a session
to expire.


### If you are asked to re-authenticate later

The integration renews its login token on its own. If it cannot - for example
the refresh token expired because the car was offline for a long time - it says
so in **Settings -> Repairs**, with a card named after the vehicle:

> **XPENG login expired for XPENG 123456 (VIN L1NNS…)**

The card explains the situation and offers a **Fix** button that opens the
import form directly, so a fresh pair can be pasted without hunting through
settings. What it asks for:

1. Re-run the onboarding tool from [the alternative](#alternative-mint-the-first-pair-yourself)
   and drag the slider once.
2. Paste the new `xpeng_tokens.json` and `xpeng_tenant.json` into the card.

**Import the pair you just produced, and only that one.** Every successful
refresh rotates the refresh token, so any older `xpeng_tokens.json` - including
the one that was working yesterday - is already retired. Re-importing it does
not bring the old session back, it evicts the live one, which looks exactly like
"the fix did not work". One run of the tool, imported once.

If the card is not there - it is deleted when a fix is submitted, and an update
does not put it back - use Home Assistant's own prompt instead: the
**Authentication expired for XPENG …** notification opens the same import form
for an entry that was created by **Import tokens**. Both routes update the
existing entry; there is no need to delete and re-add it.

The card clears itself once the new pair works. The in-app email/password
re-auth cannot work, because it hits the same captcha.

The certificates from step 2 do **not** need redoing unless XPENG rotates them,
which shows up as a TLS or broker authentication failure rather than a login
failure.

### Notes

Each installation mints its **own device identity**, and the credentials issued
are bound to it, so a token set from step 5 belongs to whichever machine and
install it was produced for. That is why the pair is imported together - the
tenant records the identity the tokens were sealed against. Do not mix tokens
from one run with a tenant from another.

### The message centre uses a different certificate authority

Worth knowing if you ever chase a notification problem. `apps-global.xiaopeng.com`
is signed by the XPENG CA chain shipped with this integration, so that endpoint is
verified against the pinned chain alone.

`msgcenter-oversea-eur.xiaopeng.com` is **not** - it sits behind Aliyun's WAF and
serves a public certificate. The integration therefore adds the system CA store
*in addition to* the pinned chain for that endpoint only. Pinning it to the XPENG
chain as well made every message-centre call fail with

```
SSL: CERTIFICATE_VERIFY_FAILED - unable to get local issuer certificate
```

while the vehicle API kept working, which is a confusing combination to debug.
Verification is not weakened for anything else: `check_hostname` and
`CERT_REQUIRED` still apply, and the XPENG-chain endpoints are still pinned.

### Some data is gated to the owner account

XPENG returns different data depending on the account's relationship to the car,
and it does not always say so:

| Data | Behaviour on a secondary (shared) account |
|---|---|
| Energy consumption | Endpoint returns **HTTP 200 with `0.0`** - no error at all |
| Location | Still delivered - the **app** hides it, the API does not |
| Some endpoints | `5000000 用户无权限` / `用户不可见` ("no permission" / "not visible") |

Measured 2026-10-09: the owner's app showed **`16.9 kWh/100km` - 7-day average
energy consumption**, while `/flow/cv2/app/vehicle/energy/simpleReport` returned
exactly `avgTripEnergyPowerOf7Days: 0.0` for the secondary account.

**So a `0.0` on the energy sensor is not proof of zero consumption.** That sensor
carries a `zero_may_mean_restricted` attribute when the value is zero, saying so
explicitly. If you care about consumption figures, onboard with the **owner**
account.

Worth knowing in the other direction too: location is *not* gated by the API, so
this integration will show a car's position even when the app shows it only to
the owner. That is a deliberate consequence of using the API rather than the UI,
not a bug - but be aware of it if the car is shared.

### Which account is this?

There is a diagnostic **Account** sensor showing the XPENG account the stored
credentials belong to. It is useful when you run more than one account, or after
a re-onboard, to confirm which one is actually connected.

The tokens cannot answer this themselves — an access token is a tenant-sealed
blob that contains only the teeId and this install's device id, and the tenant
holds keys rather than account details. So the email is recorded at the moment it
is known: the onboarding tool writes it into the token file, and the import step
stores it on the config entry, which is what survives a reboot.

Entries imported before the tool recorded it have no email; the sensor then shows
the numeric account id and an attribute explaining that re-running the onboarding
tool (or filling in the field on the **Import tokens** form) will supply it.

Note the value is the full account address, which is the point of showing it — so
it will appear in dashboards and in the recorder database.

## Troubleshooting

### Most entities are `unknown`

The usual cause, and the one worth ruling out first, is that **two MQTT clients are
connected under one XPENG account.** The broker authorises one and refuses the other as
`Not authorized`; every refusal triggers a credential refresh and a client rebuild,
and each rebuild resets the entities - so values appear, vanish, and the count
flaps around instead of settling.

```
XPENG MQTT connection refused: rc=Not authorized (rc_str=Not authorized)
```

The usual way in is **changing `device_id` on an install that already had one**.
That changes the MQTT client id, and the client still using the old one is not
always torn down - so two clients for one account end up fighting.

**Fix: restart Home Assistant.** A config-entry reload does *not* clear it. The fix
in the integration (0.4.6) stops a previous client when an entry is set up again,
but if you are already in this state, only a restart clears the one that is loose.

Two things that reliably mislead when you are debugging this:

- **`sensor.<vehicle>_mqtt_listener` reads `No messages` on a perfectly healthy
  install**, because vehicle data arrives over the REST poll rather than that
  topic. Do not read it as "MQTT is dead, so there is no data".
- **Counting client ids in the log tells you about history, not about now.** To
  see which client is live, compare the *newest* timestamp per client id instead
  of how many appear.

### `Polling status` is `Silent` on a parked car

`Silent` means "connected, but nothing has arrived for longer than the silence
threshold" - the state the watchdog reconnects for. It is not the same as
`Parked`, which is the normal quiet state. Because the parked poll interval is
measured in hours while the silence threshold is minutes, a parked car can sit in
`Silent` and have the watchdog reconnect between polls; a value that returns to
`Parked` is not a problem in itself. If a car you know is awake stays `Silent`
alongside `Not authorized` refusals, treat it as the case above.

### Nothing is updating, and the log does not say why

- **The renewal app records the whole onboarding run** - every step, plus whatever
  the browser or the API said - in `/config/xpeng-renew-last-run.log`, rewritten on
  every run. It is the first place to look, and it describes the account's login
  attempts, so treat it as sensitive.
- **`GET http://<your-ha-host>:8090/healthz`** reports renewals, failures and the
  last outcome, including what `xpeng.import_credentials` returned.
- **The Home Assistant log** shows `homeassistant.components.rest_command` errors,
  which name the status the renewal app answered with.

Note that Home Assistant 2026.10 no longer exposes persistent notifications as
`persistent_notification.*` entities, so a template that watches for the renewal
app's failure notification will not find it - look in the notifications panel.

### The energy sensor reads `0.0`

That is usually not zero consumption but a restricted endpoint on a shared account
- see [Some data is gated to the owner account](#some-data-is-gated-to-the-owner-account).

## Options

- **Certificate directory** - where your mTLS files live (defaults to the
  integration's `certs/` folder)

## Disclaimer

Not affiliated with, endorsed by, or supported by XPENG. Built by
reverse-engineering the official mobile app; it may break at any app release.
Use your own account and credentials, at your own risk. A third-party client may
affect the official app's live view.
