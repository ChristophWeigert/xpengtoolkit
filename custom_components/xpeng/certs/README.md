# XPENG mTLS Certificates

This directory holds the three files used for mutual TLS (mTLS) to the XPENG
MQTT broker `iot-app-global.xiaopeng.com:8885`. These files are NOT included in
git: the repository ignores `custom_components/xpeng/certs/*.pem`,
`*.crt`, `*.der` and `*.bks`, so they stay local.

## Required Files

| File | Description |
|------|-------------|
| `ca-chain.crt` | Xiaopeng Motors CA certificate chain |
| `client-cert-0.pem` | Client certificate (EC P-256) |
| `client-key.pem` | Client private key (EC P-256) |

All three are required. The MQTT client verifies the broker's certificate
against `ca-chain.crt` (peer verification and hostname checking are enabled), so
a missing or wrong CA file is a hard error, not a silent downgrade.

## Where the files are read from

The directory is configurable — you no longer have to drop keys inside the
integration folder, where a HACS upgrade would overwrite them:

- **Options option**: *Settings → Devices & Services → XPENG → Configure*, the
  **Certificate directory** field (options key `cert_dir`).
- **Default**: `DEFAULT_CERT_DIR` =
  `/config/custom_components/xpeng/certs` — i.e. this directory.
- Leaving the field **blank** also falls back to this integration's own
  `certs/` directory.

A path outside the integration directory is recommended, e.g.
`/config/xpeng-certs`, because an integration update reinstalls
`custom_components/xpeng/`. The options form validates the directory: it must
contain all three files, otherwise saving is refused with a form error.

## Extraction

**You can get all three files from the Android APK, offline, with no device,
no jailbreak and no Frida.** Use `tools/xpeng_apk_certs.py`:

```bash
pip install pyjks cryptography

# straight from the package - finds the keystore and the password, writes PEMs
python3 tools/xpeng_apk_certs.py --apk XPENG_3.12.0_APKPure.xapk --out ./certs

# inventory only: certificates read without any password
python3 tools/xpeng_apk_certs.py --apk base.apk --list
```

It writes exactly the three files above and then checks that the extracted
private key really is the key for the extracted certificate. Exit codes: `0`
complete, `2` partial (certificate found but key not recovered), `1` bad input.

### How that works

The app ships its client identity as a Bouncy Castle **BKS** keystore at
`assets/client0619.bks`. The important detail:

1. **Certificates in a BKS are not encrypted** — only the private key is
   PBE-wrapped. So the certificate half needs no password at all.
2. **The keystore password is `xiaopengapp`.** It is the app's documented
   `client_crt_pwd` configuration value. Note the APK contains the config *key*
   (`client_crt_pwd`, in `config.arm64_v8a.apk!lib/arm64-v8a/libdatajar.so`) but
   **not** the password value — the value is documented externally.
3. Given the password, the key unwraps in pure Python (`pyjks`). The XPENG
   keystore wraps the key in a `BksSealedKeyEntry`, which must be decrypted once
   before the inner entry holding the PKCS#8 key is reachable.

Earlier tooling in this project claimed the BKS password was "in packed/encrypted
DEX" and therefore not statically recoverable. **That was wrong**, and the
correction is documented in [](../../../).

### Version boundary — read this before you download an APK

| XPENG build | Extractable from the APK? |
|---|---|
| **3.7.0 → 3.12.0** | **Yes** — all carry a byte-identical keystore and CA chain |
| **3.13.0 → 3.15.x** | **No** — different keystore with no readable certificate and no `BKS` marker |

The break is exactly between **3.12.0 and 3.13.0**, and it is a deliberate
hardening change: from 3.13.0 the client certificate, its password *and its type*
are supplied as runtime configuration rather than shipped as a self-contained
keystore. For those builds the APK alone is not enough and you need another
route. Bundle size is **not** a signal — 3.12.0 is small and works, 3.13.0 is
the same size and does not.

Full tested matrix and the evidence: [](../../../).

### Other sources

- **Mac users:** the installed app bundle contains the same identity as an
  encrypted `xpapp-client-01.ecc.p12`. **Its password is not needed** — the same
  certificate and key are already obtainable unencrypted from the Android
  keystore, which is what that p12 wraps. See
  [](../../../).
- **iOS:** if the material is shipped as plain PEM/DER under
  `Payload/XPENG.app/Frameworks/App.framework/certs/*`, point the tool at the
  unpacked `.ipa` or that directory and it will read it directly.
- **Simplest of all:** copy the three files from a working XPENG MQTT setup you
  already control.

## What is secret, and what is not

- `ca-chain.crt` is a **public trust anchor** — it is the XPENG certificate
  authority chain and is safe to share.
- `client-cert-0.pem` and `client-key.pem` are **app-level secrets** extracted
  from the official app. You must supply them yourself: this project
  deliberately does not distribute them. Keep the private key readable only by
  the HA user, never commit it, and never paste it into a chat or an issue.

The client identity is **app-wide and shared** — its subject is
`CN=xp_app_client01`, the same for every user of the official app; it is not
tied to your account. XPENG can rotate it at any app release, at which point
the old cert/key stop working and you must re-extract from the newer app
package.

## Disclaimer

This integration is **unofficial** and is **not affiliated with, endorsed by,
or supported by XPENG**. It was reverse-engineered without official support,
so it may break at any app release or server-side change with no notice. Use
your own account at your own risk, and be aware that connecting a second client
can affect the official app's live view (it may log the app out or show stale
data). No key material is distributed with this project.
