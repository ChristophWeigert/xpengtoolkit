# Windows setup (WSL2) — onboarding an XPENG vehicle from a Windows PC

Everything the **Xpengtk** Home Assistant integration needs *outside* Home
Assistant: the mTLS certificates and the account tokens. Adapted from notes by
**A user who did this on Windows first** — including the Samba-share
trick in Phase 6, which is genuinely convenient. No Linux experience is assumed;
every command is copy-pasteable.

**Why WSL?** One step of the XPENG sign-in is a slider captcha only a human
dragging it in a real browser can clear. The tool that handles it is a Python +
Node script, which is happiest on Linux — WSL2 gives you that without a second
machine.

---

## Read this first: the repo layout

```
xpengtoolkit/
├── custom_components/xpeng/     <- the integration HACS installs
├── tools/                       <- the scripts you run
│   ├── xpeng_apk_certs.py       <- extracts the mTLS certs from the APK
│   ├── onboard_email_login.py   <- mints the account tokens
│   ├── solve_captcha.mjs        <- opens the slider window
│   └── guardian_token.mjs
└── README.md
```

`tools/` and `custom_components/xpeng/` are **siblings at the repo root**, and
`onboard_email_login.py` imports Python files out of `custom_components/xpeng/`.

**Download the whole repository (Code → Download ZIP) and run every command from
the extracted repo root.** GitHub names the extracted folder `<repo>-main`
(here `xpengtoolkit-main`); from there `tools/...` and
`custom_components/xpeng/...` both resolve and nothing needs moving. Only
`xpeng_apk_certs.py` is fully standalone — the onboarding script is not.

Both layouts now work: a full checkout, *or* a `tools/` directory placed inside
`custom_components/xpeng/`. The scripts detect which one they are in and
otherwise print an actionable error, but the full checkout is recommended.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows 10 (2004+) or 11, 64-bit | Administrator access needed in Phase 3 |
| A **secondary** XPENG account | From Phase 1 — never the account on your phone |
| Home Assistant reachable on your network | Hostname/IP, plus its Samba add-on for Phase 6 |
| ~3 GB free disk | WSL/Ubuntu, Node, and the ~164 MB APK |
| Your vehicle's **VIN** | 17 characters, from the XPENG app or the windscreen |

**Use a dedicated account.** XPENG allows one active session per account, so the
integration and your phone app would otherwise log each other out repeatedly.
That is not fixable here — it is how the account model works.

Replace throughout: `YOUR-WINDOWS-USERNAME` (shown in Windows paths as
`C:\Users\<you>\...`), `YOUR_SECONDARY_EMAIL` / `YOUR_PASSWORD` (Phase 1), and
`<ha-host>` (your Home Assistant hostname or IP).

---

## Phase 1 — the secondary account

1. At <https://login.xpeng.com/> create a **new** account with a **different
   email** from your primary one.
2. In the official XPENG app, signed in as your **primary** account, share the
   car to it (Vehicle → sharing / "share to another user"; wording varies by
   region).
3. Sign in as the new account and **accept** the share.
4. **Log in to the official XPENG app at least once with the secondary
   account.** Easy to skip, and it matters: a freshly shared car has no vehicle
   state until the real app has read it, so the minted credentials would return
   no data.

Keep that email and password to hand — Phase 5 uses them.

---

## Phase 2 — install the integration via HACS (do not configure it yet)

1. In Home Assistant: **HACS → Integrations**.
2. Three-dot menu → **Custom repositories**.
3. Add `https://github.com/xpengtoolkit/xpengtoolkit`, category **Integration**.
4. Install **Xpengtk**, then **restart Home Assistant**. *(If it is listed in
   the HACS default store you can just search for `Xpengtk`; the
   custom-repository route always works.)*

This creates `/config/custom_components/xpeng/` on the Home Assistant host. Do
**not** add the integration under Settings yet — its tokens do not exist until
Phase 5.

---

## Phase 3 — WSL2 and its dependencies

In **PowerShell as Administrator**, enable the Windows features (the second is
what makes WSL2 possible):

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
```

**Restart Windows**, then in a new PowerShell window:

```powershell
wsl --install -d Ubuntu
```

When the Ubuntu terminal opens, create a Linux username and password (local to
WSL, unrelated to Windows), then install the dependencies:

```bash
sudo apt update && sudo apt install python3-pip build-essential python3-dev nodejs npm -y
pip3 install pyjks cryptography paho-mqtt --break-system-packages
```

`--break-system-packages` is needed on Ubuntu 24.04 and newer, which refuses
`pip3 install` outside a virtual environment (PEP 668). It is safe here.

Finally, the browser pieces used **only for the single slider drag**:

```bash
mkdir -p /tmp/pwdir && cd /tmp/pwdir && npm init -y && npm install playwright-core
npx playwright install chrome
npx playwright install chromium
```

Node and Chrome are needed for nothing else — the login itself is plain HTTPS.

---

## Phase 4 — get the repository and the APK onto the PC

Create one working folder, e.g. `C:\Users\<you>\Downloads\xpeng`, and put two
things in it:

- **The whole repository** — on GitHub, **Code → Download ZIP**, then extract it,
  giving `C:\Users\<you>\Downloads\xpeng\xpengtoolkit-main\`.
- **The APK** — search for **"XPENG 3.12.0 APKPure"** and download
  `XPENG_3.12.0_APKPure.xapk` (~164 MB).

> **The APK version is a hard ceiling: 3.7.0 – 3.12.0.** From **3.13.0** onward
> XPENG ships an **encrypted keystore** and the certificate can no longer be
> extracted from the package at all. 3.12.0 is the newest usable build — do not
> take a newer one because it looks better.

From WSL, Windows drives appear under `/mnt/c/`, so that folder is
`/mnt/c/Users/YOUR-WINDOWS-USERNAME/Downloads/xpeng`.

---

## Phase 5 — run the two commands

In the Ubuntu terminal, go to the **repo root** (not `tools/`, not
`custom_components/`):

```bash
cd /mnt/c/Users/YOUR-WINDOWS-USERNAME/Downloads/xpeng/xpengtoolkit-main
```

**1. Extract the mTLS certificates** from the APK. This writes `ca-chain.crt`,
`client-cert-0.pem` and `client-key.pem` into `./certs`, and verifies that the
private key really matches the certificate:

```bash
python3 tools/xpeng_apk_certs.py \
    --apk /mnt/c/Users/YOUR-WINDOWS-USERNAME/Downloads/xpeng/XPENG_3.12.0_APKPure.xapk \
    --out ./certs
```

**2. Mint the account tokens** using the Phase 1 credentials:

```bash
python3 tools/onboard_email_login.py \
    --email YOUR_SECONDARY_EMAIL \
    --save --mqtt
```

A browser window opens on the Windows desktop. **Drag the slider once.** That
single gesture is the only unscriptable part; the script registers the solved
challenge and retries the login itself.

It writes a **matched pair** into `custom_components/xpeng/` inside the extracted
repo:

- `xpeng_tokens.json` — the account tokens (note the plural, **tokens**)
- `xpeng_tenant.json` — the tenant key pool **and the per-install device id**
  the tokens are sealed to

> **Never mix files from different runs.** The tenant carries the random device
> id the token envelope was minted for and cannot be reproduced. Tokens and MQTT
> credentials from two different runs make every API call fail with `12010104
> Login status expired` — which looks exactly like an expired login but is not.

`--mqtt` also mints the initial MQTT credentials into the token file, and
requires `--save` (the MQTT account must use the same tenant as the login).

---

## Phase 6 (optional) — the Samba-share variant

To have generated files land **directly on the Home Assistant host**, install
the **Samba share** add-on in Home Assistant (Settings → Add-ons → Add-on Store),
then mount it from WSL:

```bash
sudo mkdir -p /mnt/ha_config
sudo mount -t cifs //<ha-host>/config /mnt/ha_config \
    -o username=<samba-user>,password=<samba-password>
```

If the mount fails: add `,vers=3.0` to force the dialect; add
`,uid=$(id -u),gid=$(id -g)` so your WSL user can write the files; and if the
hostname does not resolve, use the IP instead of `<ha-host>`. Note the password
lands in your shell history and in `ps` — clear it with `history -c`, and unmount
with `sudo umount /mnt/ha_config`.

With the share mounted you can point the tools at the HA host filesystem so
nothing needs copying afterwards — for example straight into the integration's
default certificate directory:

```bash
python3 tools/xpeng_apk_certs.py \
    --apk /mnt/c/Users/YOUR-WINDOWS-USERNAME/Downloads/xpeng/XPENG_3.12.0_APKPure.xapk \
    --out /mnt/ha_config/custom_components/xpeng/certs
```

Do the same with the onboarding tool (extracting the repo inside `/config/xpeng/`
and running it from there) and the token JSONs also land in the Home Assistant
config tree, where the import form can read them as `/config/xpeng/...`. Pasting
is still simpler — paths are only a convenience for files already on the HA host.

---

## Phase 7 — configure the integration in Home Assistant

1. **Settings → Devices & Services → Add Integration**, search for **Xpengtk**.
2. Enter your **Vehicle Identification Number (VIN)** and choose **"Import
   tokens from the onboarding tool (recommended)"**.
3. On the **Import tokens** form, **paste** the *entire* contents of each file:
   - **`xpeng_tokens.json` contents** — including the `json` wrapper the tool
     writes, not just the inner token object.
   - **`xpeng_tenant.json` contents** — the entire file.
   - **VIN** — already filled in above; leave it unless the form is empty.

   Pasting is the primary method and avoids every filesystem issue. The two
   `... path (instead of pasting)` fields are the alternative; a path there is
   read **on the Home Assistant host**, i.e. inside the container, so it looks
   like `/config/...` — not like a Windows or WSL path.
4. Finish the dialog. The integration **adopts the device id from the imported
   tenant** rather than minting a new one.
5. On the integration's card choose **Configure** (Options) and set
   **Certificate directory** to the folder holding the three PEM files. The
   default is `/config/custom_components/xpeng/certs`; if you generated them
   into the repo (`<repo>/certs`) or via Samba, point it at wherever they are.

The sensors, switches and buttons should populate. Nothing in `tools/` is needed
again — the imported tokens refresh themselves through the API.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python3: command not found` | WSL/Ubuntu is not installed, or you are in PowerShell rather than the Ubuntu terminal. `wsl -l -v` lists your distros. |
| `error: externally-managed-environment` from `pip3` | Ubuntu 24.04 PEP 668 protection — add `--break-system-packages` to the `pip3 install` line. |
| Cert extraction exits **2** with `no candidate password opened the XPENG keystore` | The APK is **3.13.0 or newer**, whose keystore is encrypted. Only **3.7.0–3.12.0** works; download 3.12.0. |
| No slider window appears *and you passed `--captcha-solver`* | Expected: with a solver the tool makes one HTTP call instead of opening a browser. Check `curl -s http://your-solver:8080/healthz`, the token, and that `guardian_token.mjs` is still present (it is needed on both paths). |
| No slider window appears | Chrome/Playwright missing in WSL — re-run `npx playwright install chrome`. Check the helpers are found: the tool walks up from itself looking for `solve_captcha.mjs` and `guardian_token.mjs`, overridable with `--tools-dir /path/to/repo/tools` or `XPENG_TOOLS_DIR=/path/to/repo/tools`. |
| `cannot find the integration's Python files next to this tool` | You downloaded only `tools/` instead of the whole repository. Unzip the **whole** repo and run from its root, or place `tools/` inside `custom_components/xpeng/`. |
| Integration says the login expired after a while | Expected once the refresh token ages out. Re-run the Phase 5 command and re-import the **new pair**. |
| `12010104 Login status expired` right after importing | Tokens and tenant came from different runs, or the tenant did not come through. Re-run and import both files from that one run. |

---

## What is manual, and what is not

The genuinely human-only parts are (1) creating the secondary account and
sharing the car to it, (2) logging into the official app once with it, and
(3) **one slider drag** in the browser window Phase 5 opens. Everything else —
certificate extraction, tenant fetch, token minting, MQTT account creation — is
scripted. Only the Phase 3 dependency install is otherwise manual, so a future
`install.sh` could chain the apt/pip/npm setup and both Phase 5 commands into a
single entry point. (suggested during that trial; not implemented yet.)

---

## See also

- [`README.md`](../README.md) — the main setup guide and the why behind each step
- [`tools/README.md`](../tools/README.md) — the onboarding tool in detail
-  — how the extraction
  works and which APK versions are covered
