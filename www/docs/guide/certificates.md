# Certificates

XPENG's app tier authenticates with mutual TLS: the client presents a certificate
and a private key, and the server verifies them. That identity is **app-wide, not
per-account** — its subject is `CN=xp_app_client01`, the same for every user of the
official app — which is why the same three files work for anyone who can extract
them.

::: warning No credentials, certificates or keys are supplied
This project ships **none** of them. You obtain the mTLS client certificate and
key from your own copy of the official app, and your own account credentials,
using the instructions below. How you obtain them, where you store them, and any
distribution you make of those artefacts, is your own responsibility and
entirely at your own risk.
:::

## Why the client key is needed

Two channels need the client key; a third does not:

| Endpoint | Needs the client key? | What it can do |
| --- | --- | --- |
| MQTT broker (`iot-app-global`) | **Yes** | Real-time telemetry and vehicle control |
| App REST API (`apps-global`) | **Yes** | Account login, mints the MQTT credentials |
| Web SSO (`iot-web-eur`) | No | **At most** energy-report/dashboard data |

If you cannot obtain the key, this integration is not usable. For most people the
APK route below makes that a non-issue.

## The three files

| File | What it is |
| --- | --- |
| `ca-chain.crt` | Xiaopeng Motors CA certificate chain |
| `client-cert-0.pem` | Client certificate (EC P-256) |
| `client-key.pem` | Client private key (EC P-256) |

All three are required. The MQTT client verifies the broker's certificate against
`ca-chain.crt`, with peer verification and hostname checking enabled, so a missing
or wrong CA file is a hard error rather than a silent downgrade.

## Which APK builds work

You can get all three files from the Android APK, **offline, with no phone, no
jailbreak and no Frida**.

| XPENG build | Extractable from the APK? |
| --- | --- |
| **3.7.0 → 3.12.0** | **Yes** — all carry a byte-identical keystore and CA chain |
| **3.13.0 → 3.15.x** | **No** — different keystore with no readable certificate and no `BKS` marker |

The break is exactly between **3.12.0 and 3.13.0**, and it is a deliberate
hardening change: from 3.13.0 the client certificate, its password *and its type*
are supplied as runtime configuration rather than shipped as a self-contained
keystore. For those builds the APK alone is not enough and you need another route.
Bundle size is **not** a signal — 3.12.0 is small and works, 3.13.0 is the same
size and does not.

**Download an older build on purpose.** 3.12.0 is the newest usable version.

## Extraction

Run the tool from the top folder of the repository: it imports code from the
integration, so the layout has to stay intact. Download the whole repository (on
GitHub, **Code → Download ZIP**, then unzip) — do not download `tools/` on its own,
which is the commonest mistake and will not run.

```bash
pip install pyjks cryptography
python3 tools/xpeng_apk_certs.py --apk XPENG_3.12.0_APKPure.xapk --out ./certs
```

One command against an APK build in the supported window writes `ca-chain.crt`,
`client-cert-0.pem` and `client-key.pem`, and checks that the key matches the
certificate before declaring success.

| Exit code | Meaning |
| --- | --- |
| `0` | Complete — certificate and key recovered, and they match |
| `2` | Partial — a certificate was found but the key could not be recovered, which is what a 3.13.0-or-newer package looks like |
| `1` | Bad input |

An inventory-only run reads the certificates without any password:

```bash
python3 tools/xpeng_apk_certs.py --apk base.apk --list
```

::: details How the extraction works
The app ships its client identity as a Bouncy Castle **BKS** keystore at
`assets/client0619.bks`. Certificates in a BKS are not encrypted — only the
private key is PBE-wrapped — so the certificate half needs no password at all.
The keystore password is `xiaopengapp`, the app's documented `client_crt_pwd`
configuration value: the APK contains the config *key* but not the password
*value*, which is documented externally. Given the password, the key unwraps in
pure Python (`pyjks`). The keystore wraps it in a `BksSealedKeyEntry`, which must
be decrypted once before the inner entry holding the PKCS#8 key is reachable.
:::

::: details Other ways to obtain the same identity
- **macOS** — the installed app bundle contains the same identity as an encrypted
  `xpapp-client-01.ecc.p12`. Its password is not needed: the same certificate and
  key are already obtainable unencrypted from the Android keystore, which is what
  the p12 wraps.
- **iOS** — if the material is shipped as plain PEM/DER under
  `Payload/XPENG.app/Frameworks/App.framework/certs/*`, point the tool at the
  unpacked `.ipa` or at that directory and it reads it directly.
- **Simplest of all** — copy the three files from a working XPENG MQTT setup you
  already control.
:::

On Windows, the repository's
[`docs/WINDOWS_WSL_SETUP.md`](https://github.com/xpengtoolkit/xpengtoolkit/blob/main/docs/WINDOWS_WSL_SETUP.md)
walks this whole process through WSL2, including the APK download.

## Where to put them

Extraction ran on your own computer; the integration runs on Home Assistant, so
the three files have to get there. Put them **outside** the integration folder:

```text
/config/xpeng-certs/
├── ca-chain.crt
├── client-cert-0.pem
└── client-key.pem
```

`/config/xpeng-certs` is what the rest of this documentation assumes. Any path
works except `custom_components/xpeng/certs/`: that is the default, but it sits
inside the integration, which an update replaces — your keys would go with it.

Getting them there is up to you and your setup: the **Samba share**, the **File
editor** app, `scp`, or anything else that can write to `/config`. Then check they
arrived:

```bash
ls -l /config/xpeng-certs      # three files
```

These are live credentials, so set the directory to `700` and the files to `600`:

```bash
chmod 700 /config/xpeng-certs
chmod 600 /config/xpeng-certs/*
```

Do not leave the folder exposed in a share.

## How the integration finds the directory

At startup the integration probes a short list and uses the first candidate that
actually contains `ca-chain.crt`:

| Order | Candidate | Notes |
| --- | --- | --- |
| 1 | The **Certificate directory** option (`cert_dir`) | used only if the file is there |
| 2 | `certs/` beside the integration module | under Home Assistant that is `/config/custom_components/xpeng/certs`; a vendored copy of the module (the renewal app carries one) resolves to whatever sits beside it |
| 3 | `/config/xpeng-certs` | tried last, so an install already resolving to the integration's own folder keeps doing so |

**Nothing needs configuring for either of the two common locations.** Only if you
keep the three files somewhere else do you need to say so: **Settings → Devices &
Services → XPENG → Configure → Certificate directory**. Leaving the field blank
falls back to the order above. The form validates the directory and refuses to
save unless all three files are in it.

## Not shipped with the project

The client certificate and private key are **not** distributed with this project;
you extract them from your own copy of the official app. The repository ignores
`custom_components/xpeng/certs/*.pem`, `*.crt`, `*.der` and `*.bks`, so extracted
files stay local.

- `ca-chain.crt` is a **public trust anchor** — it is the XPENG certificate
  authority chain and is safe to share.
- `client-cert-0.pem` and `client-key.pem` are **app-level secrets**. Keep the
  private key readable only by the Home Assistant user, never commit it, and never
  paste it into a chat or an issue.

XPENG can rotate this identity at any app release, at which point the old
certificate and key stop working and you must re-extract from a newer package.
That shows up as a TLS or broker authentication failure rather than a login
failure.
