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
| `probe_seat.py` | Measure what a seat command does on your own car, printing the state the car reports around each one. `--sequence` runs heat 1/2, ventilation 1/2 and off. Worth running before trusting any of the seat code: the encodings are momentary switches, `cmd_type` is 1 = driver / 0 = passenger, and the car must be woken after a command or it reports nothing. |


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
