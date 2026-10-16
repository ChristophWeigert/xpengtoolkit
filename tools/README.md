# tools/ — onboarding a vehicle, outside Home Assistant

## Why this step is external

XPENG gates sign-in with its own **slider captcha** (NetEase Yidun). The server
answers the email/password step with `12040101` (slider demanded) and
`12040106` (`Slider captcha validation failed`) until a human drags it. That
drag needs a real browser window, so it **cannot be solved from inside Home
Assistant** — the config flow has no way to render or complete it.

The integration is therefore honest about it: the email/password and reauth
steps stop with a captcha-gated message instead of failing generically, and the
credentials are minted here, on a machine with a browser, then imported into HA
through the **Import tokens from the onboarding tool** method.

Nothing in this directory is needed again after onboarding: the imported tokens
refresh themselves through the API, and no captcha is ever seen in HA.

## `onboard_email_login.py` — what it does

It replays XPENG's own email/password login over plain HTTPS (no app, no root,
no phone):

1. fetches a tenant (the key pool this install's credentials are sealed with);
2. asks `getUserStateByEmail` for a login session id (`xid`);
3. attempts `pwdLogin`;
4. **if the slider gate is up, it opens a browser window — you drag the slider
   once** — and registers the solved challenge against that same session;
5. retries `pwdLogin` and receives the real account tokens;
6. writes `xpeng_tokens.json` **and** `xpeng_tenant.json` as a matched pair;
7. with `--mqtt`, also mints the initial MQTT credentials and writes them into
   the token file;
8. records the **VIN** and the account email in the token file, so nothing has to be
   typed at import time - the VIN decides the entity ids, and it is the one piece of
   identity the pair cannot carry. The import flow reads it from there. Best effort:
   if the vehicle list cannot be read, the login is still saved and the form simply
   asks for the VIN.

### Requirements

| Needed for | Requirement |
|---|---|
| The login itself | Python 3, `cryptography`, `paho-mqtt` (the component's `xpeng_api.py` is loaded by path) |
| **Only the slider drag** | `node`, `playwright-core`, and a real Chrome/Chromium |
| The silent anti-bot retry | `node` + Playwright too (`guardian_token.mjs`) |

So Node and Playwright are needed only because the human drag happens in a real
browser. A machine that is never captcha-challenged would not need them — in
practice XPENG challenges almost every account, so treat them as required.

One-time setup (any directory works; `--playwright-dir` defaults to `/tmp/pwdir`):

```bash
mkdir -p /tmp/pwdir && cd /tmp/pwdir && npm init -y && npm install playwright-core
```

### Run it

**Run it from the repository root, with the checkout intact.** This script
imports `xpeng_api.py`, `guardian.py`, `const.py` and `xpeng_errors.py` from
`custom_components/xpeng/`, so the layout matters:

```
xpengtoolkit-main/            <- run commands from here
├── custom_components/xpeng/  the integration (the scripts import from it)
└── tools/                    this directory
```

Downloading `tools/` on its own does **not** work. If the script cannot find the
component it says so and names the paths it tried, rather than dying with a bare
`ImportError`. It also accepts the layout where this `tools/` directory has been
copied *inside* `custom_components/xpeng/`, which is what you get if you place it
on the Home Assistant share.

```bash
# asks for the password on stdin (nothing in shell history)
python3 tools/onboard_email_login.py \
    --email USER@example.com \
    --save --mqtt
```

A browser window opens; **the user drags the slider once**. The script prints a
live `createMqttAccount` clientId on success and writes
`xpeng_tokens.json` (note the plural) and `xpeng_tenant.json` beside the
integration files the run used, mode `0600` - `custom_components/xpeng/` in a
checkout, which is what this route pastes from.

The one layout that is not a checkout is a run **on the Home Assistant host
against the HACS-managed copy** (`/config/custom_components/xpeng`). HACS replaces
that directory on every update, so a pair written into it is gone at the next one -
and the integration then asks for a pair the user already had. The script detects
that directory and writes to `/config/xpeng-secrets/` instead, where the renewal
app publishes its own and what the integration's import form looks in first.
`--out-dir PATH` chooses a directory explicitly; the script prints where it wrote
either way.

> `--mqtt` requires `--save`: the MQTT account must be minted with the same
> tenant that was used for the login, never a re-loaded cache.

### Where the Node helpers live

`solve_captcha.mjs` and `guardian_token.mjs` live in the **repository's
top-level `tools/` directory**, not in this one. The script finds that directory
by walking up from its own location, so it works both from a checkout
(`<repo>/xpeng-google/tools/`) and from a checkout's own `tools/`. Override it
when the layout is different:

```bash
--tools-dir /path/to/repo/tools      # or: XPENG_TOOLS_DIR=/path/to/repo/tools
```

If the helpers cannot be found the script says so, naming the directory it
looked in, before opening any browser.

## Import the result into Home Assistant

Settings → Devices & Services → Add Integration → XPENG, choose **Import tokens
from the onboarding tool**, then paste the contents of:

| File | Must contain |
|---|---|
| `xpeng_tokens.json` | `accessToken`, and an `mqttAccount` or `uid` (the tool wraps the tokens as `{"json": {...}}`; the wrapper is accepted). Also carries `vin` and `account_email` when the tool could read them |
| `xpeng_tenant.json` | `deviceId` and a non-empty `keys` pool (base64) |

Their paths can be given instead, when the files are already on the HA host.

What the import stores, and why:

- **both files go into the config entry data** (not into the legacy
  `token_file` / `tenant_file` options), so the credentials have exactly one
  source and cannot drift out of sync with a file on disk;
- the **tenant's `deviceId` becomes the entry's `configured_device_id`** — the
  key pool is sealed with `md5(deviceId)` and the tokens are bound to it, so a
  freshly minted id would make every call fail `12010104 Login status expired`;
- the MQTT client id comes from the token file's `mqttAccount`, or is built as
  `xmart:app:<uid>:<deviceId>`; an empty password is fine, the integration
  mints one on its first refresh.

**Do not move or copy the two credential files between installs.** They are
bound to one device identity, they are live account credentials, and the tool
never prints their contents — keep it that way when sharing logs.

## Other helpers in this directory

These are diagnostic/alternative paths, not part of the supported onboarding
flow:

| File | Purpose |
|---|---|
| `configure_ha_entry.py` | Build (or install into `.storage/`) the config entry from an already-written token pair. Superseded by the import step, but still useful for scripted installs. |
| `solve_and_login.py` | Interactive email+password + human slider in one script (predecessor of the onboarding tool). |
| `mqtt_credential_check.py` | Verify a saved token pair can mint a working MQTT connection. |
| `xpeng_can_capture.py` | Record the broker's raw traffic to a binary journal, outside Home Assistant. See [below](#recording-the-can-stream-and-decoding-it-offline). |
| `xpeng_can_decode.py` | Read that journal offline: counts, payload shapes, hexdumps, JSON export, pluggable decoder. |
| `probe_seat.py` | Measure what a seat command does on your own car, printing the state the car reports around each one. `--sequence` runs off, then heat 1/2/3 and ventilation 1/2/3; `--seat rear-left --scan-cmd-types` tries the rear candidates and names the state field each one moves, and `--cmd-type N` sends one you know. Worth running before trusting any of the seat code: `mode` is the level (0 = off, 1/2/3), `cmd_type` is 1 = driver / 0 = passenger / 3 = left rear / 4 = right rear (measured 2026-10-14 - the scan re-derives the rear two on a car that differs), and the car must be woken after a command or it reports nothing, because a seat command takes 7-50 s to show up. |


## Recording the CAN stream, and decoding it offline

`mqtt_topic_discovery: true` subscribes to the known CAN topics, and
`mqtt_listener_topics: "#"` subscribes to the entire broker on top - including the
car's 1 Hz `CanPack1s` broadcast. That combination wedged Home Assistant on
0.4.30, because every frame meant a decode attempt on binary data, an HA event and
a full update of every entity, which the recorder then wrote. 0.4.32 coalesced
that work to one update per second, which fixes the wedge but is the wrong answer
for a *recording*: per-frame timing is the thing a recording is for.

There are two ways to keep the stream, and the choice turns on one fact - **XPENG
authorises one MQTT session per account**:

| | Where the frames come from | What it needs |
|---|---|---|
| **`capture_enabled`**, inside the integration | the connection Home Assistant already holds | nothing extra - it coexists by construction |
| **`xpeng_can_capture.py`**, below | its own MQTT session on the XPENG broker | a second XPENG account with the car shared to it |

**Use the integration option** when the capture is for the car you already have in
Home Assistant. There is no second session to authorise, nothing decodes on the way
in, and the per-frame cost is a single enqueue onto a fixed-size queue - if the
disk falls behind, frames are **dropped and counted** rather than queued without
limit, which is what makes `mqtt_listener_topics: "#"` survivable where 0.4.30 was
not. It writes a journal under `capture_path` (default `/share/xpeng-can`), and the
counts are on `sensor.xpeng_<vehicle>_capture`. Both options are documented under
[Integration options](../www/docs/reference/options.md#raw-capture).

**Use the tool below** on a machine that is not the Home Assistant host, or to hunt
with a dedicated account. It mints its own session from the credentials the install
already holds, subscribes to the same topics the integration's discovery tries, and
writes every frame verbatim. Decoding is the same either way:

| File | Purpose |
|---|---|
| `xpeng_can_capture.py` | Mints its own MQTT session, subscribes, and writes every frame verbatim to a binary journal. It never decodes. |
| `xpeng_can_decode.py` | Reads a journal offline - no broker, no credentials, no car: counts and rates, distinct payload shapes, hexdumps, NDJSON export, and a `--decoder module.py:function` hook for your own parser. |

```bash
python3 tools/xpeng_can_capture.py --dry-run      # the plan; no network, no paho needed
python3 tools/xpeng_can_capture.py --duration 60  # probe: coexist, or be refused

python3 tools/xpeng_can_decode.py summary xpeng-can-*.xpgcan
python3 tools/xpeng_can_decode.py shapes  xpeng-can-*.xpgcan --topic CanPack1s
```

Start an unfamiliar stream with `shapes`, not `dump`. It reduces a firehose to its
distinct payloads and prints a byte-class pattern per topic - the constant bytes,
with `..` where they vary - which is what turns "1 Hz of noise" into "16 bytes, a
counter at offset 4, everything else fixed".

Three things worth knowing before the tool's first run:

- **The probe is not destructive.** The default `--client-id-mode unique` connects
  under a client id *derived* from the minted one, so the broker either accepts both
  sessions or refuses this one - and either way the integration keeps its session.
  A refusal is recorded in the capture as `account_session_limited`, and the run
  stops after three of them rather than re-minting at the broker forever. If it is
  refused, no client id will fix it: use the integration option above, or a second
  account.
- **`--client-id-mode shared`** presents the integration's own id, which *does*
  evict it (the client id is the MQTT username, and the broker drops the older
  session with that id). That mode needs `--confirm-session-takeover`, and the
  integration should be disabled for the length of the capture.
- **Wildcard topics are broker-scoped, not car-scoped.** `Xmart/Car/+/...` is every
  car the broker will hand you, not yours. `summary` reports every car id in the
  capture, so this is visible rather than assumed, and `--vid` narrows the
  `Xmart/Car/...` candidates to one car - but do not combine `--vid` with
  `--wildcard '#'`, which is the whole broker again.

It never publishes, so it never wakes the car, unless you pass `--force-upload`.

Credentials are resolved in the order an install is most likely to have a live
pair: explicit `--tokens`/`--tenant`, then the renewal app's `--pair-dir`
(`/config/xpeng-secrets`, used only when both files are there - a capture usually
runs either side of a renewal, and that pair is the one just proven to mint), then
`--entry-file` (default `/config/.storage/core.config_entries`). So there is no
second login and no prompt. Nothing secret is printed or written into a capture:
the journal's metadata carries a VIN, a uid, a truncated device id and the broker
host, and nothing else.


A small pexpect driver used to deploy files and run commands on an HAOS box.
It exists because the obvious approaches fail in a way that looks like the
*remote* is broken, when it is the client.

Two problems it solves:

1. **Don't offer public keys.** A default `ssh` offers every local key before
   falling back to a password. Each rejected key counts as a failed
   authentication and triggers the server's per-IP penalty
   (`srclimit_penalise: ... deferred penalty`), which then delays every
   subsequent connection.
2. **Never match a shell prompt.** HAOS prints a large ASCII banner on login and
   its bash emits bracketed-paste escapes (`ESC[?2004h`), so a `[#>$] $` pattern
   never matches and the client sits until `LoginGraceTime` kills it with
   `Timeout before authentication`. The driver brackets each command with unique
   markers instead, and de-echoes the PTY.

Usage:

```bash
export HA_SSH_HOST=<ha-host>          # required
source .secrets.local                    # provides XPENG_HA_SSH_PASSWORD
```

```python
import sys; sys.path.insert(0, "tools")
```

Deploying files: **`scp` and `tar` are not trustworthy on HAOS `/config`.**
`scp` has reported success while writing nothing, and `tar -xzf` exits 0 without
overwriting existing files. Stage into `/tmp`, then `cp` into place, then compare
`md5sum` against the local file. The driver's `run()` is the reliable transport.
