#!/usr/bin/env python3
"""
xpeng_apk_certs.py - recover the XPENG mTLS client certificate and PRIVATE KEY
straight from an Android APK (or an already-extracted tree / the .bks itself).

Why this exists
---------------
The XPENG app ships its client identity as a Bouncy Castle BKS keystore,
`assets/client0619.bks`. Bouncy Castle certificates are readable without the
password, but the private key is PBE-wrapped, so the usual advice ("pull it off a
jailbroken iPhone / with Frida") was previously the only route. It isn't: the
XPENG keystore password is `xiaopengapp` (the app's documented `client_crt_pwd`),
and once you have it, pure Python can unwrap the key. No jailbreak, no Frida, no
device at all.

Scope: XPENG (`com.xiaopeng.globalcarinfo`) only.

Version coverage: works for XPENG 3.7.0 through 3.12.0, which all ship the same
keystore. From 3.13.0 onward XPENG ships an encrypted keystore and this APK-only
route no longer applies - the tool will say so rather than pretend otherwise.

Pipeline
--------
    APK/IPA/dir  or  client0619.bks
        -> locate the BKS keystore
        -> obtain its password  (supplied, or found by scanning the package)
        -> pyjks unwraps the sealed key entry
        -> emit ca-chain.crt, client-cert-0.pem, client-key.pem
        -> verify the private key actually matches the certificate

Output is byte-compatible with what the rest of the tooling expects, so the
result can be dropped into a `certs/` directory and used directly.

Requirements
------------
    pip install pyjks cryptography

Usage
-----
    python3 xpeng_apk_certs.py --apk XPENG_3.12.0_APKPure.xapk --out ./certs
    python3 xpeng_apk_certs.py --apk com.xiaopeng.globalcarinfo.apk --out ./certs
    python3 xpeng_apk_certs.py --apk client0619.bks --password xiaopengapp --out ./certs
    python3 xpeng_apk_certs.py --apk base.apk --list          # inventory only

Exit codes: 0 complete, 2 partial (missing key), 1 bad input.
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import zipfile

CA_NAME = "ca-chain.crt"
CERT_NAME = "client-cert-0.pem"
KEY_NAME = "client-key.pem"

# The XPENG keystore password, plus a few near-miss variants. The real one is
# `xiaopengapp` - the app's documented
# XPNetwork.box[*].core.production[*].client_crt_pwd.
DEFAULT_PASSWORDS = [
    "xiaopengapp",
    "client_crt_pwd",
    "xpeng", "xpengapp", "xiaopeng", "Xiaopeng", "XPENG",
    "client0619", "0619", "client", "keystore",
    "123456", "password", "changeme", "changeit", "secret", "android", "",
]

# File name fragments that indicate a client keystore.
BKS_HINT = re.compile(rb"client\d*\.bks|client\.bks|\.bks$", re.I)


# ----------------------------------------------------------------- sources --- #

def iter_zip_bytes(zf: zipfile.ZipFile):
    for info in zf.infolist():
        if not info.is_dir():
            yield info.filename, info.file_size


def find_bks(path: pathlib.Path):
    """Return (name, bytes) for the most likely client BKS in the input."""
    if path.is_file() and path.suffix.lower() == ".bks":
        return path.name, path.read_bytes()

    if path.is_dir():
        cands = [p for p in path.rglob("*.bks") if _is_clientish(p.name)]
        cands.sort(key=lambda p: (not _is_clientish(p.name), len(p.name)))
        if cands:
            return cands[0].name, cands[0].read_bytes()
        return None, None

    with zipfile.ZipFile(path) as zf:
        names = [n for n, _ in zip_bytes_names(zf)]
        bks = [n for n in names if n.lower().endswith(".bks")]
        if not bks:
            # XAPK / app bundle: the real APKs are nested zips. Descend into them.
            nested = [n for n in names if n.lower().endswith(".apk")]
            best = None
            for n in nested:
                try:
                    inner = zipfile.ZipFile(io.BytesIO(zf.read(n)))
                except Exception:
                    continue
                inner_names = [x for x, _ in zip_bytes_names(inner)]
                cand = [x for x in inner_names if x.lower().endswith(".bks")
                        and "client" in x.lower()]
                if cand:
                    cand.sort(key=lambda x: (len(x), x))
                    best = (f"{n}!{cand[0]}", inner.read(cand[0]))
                    break
            if best:
                return best
            return None, None
        # exact-name match first, then "client" in the name, then shortest.
        for want in ("assets/client0619.bks", "client0619.bks"):
            for n in bks:
                if n.lower().endswith(want):
                    return n, zf.read(n)
        clientish = [n for n in bks if "client" in n.lower()]
        pick = (clientish or bks)
        pick.sort(key=lambda n: (len(n), n))
        return pick[0], zf.read(pick[0])


def zip_bytes_names(zf: zipfile.ZipFile):
    for info in zf.infolist():
        if not info.is_dir():
            yield info.filename, info.file_size


def _is_clientish(name: str) -> bool:
    n = name.lower()
    return "client" in n and n.endswith(".bks")


def find_asset(zf_or_dir, wanted_suffixes):
    """Locate a CA-chain style asset in the package."""
    if isinstance(zf_or_dir, zipfile.ZipFile):
        names = [n for n, _ in zip_bytes_names(zf_or_dir)]
        for want in ("assets/ca-chain.crt", "ca-chain.crt"):
            for n in names:
                if n.lower().endswith(want):
                    return n, zf_or_dir.read(n)
        for n in names:
            if "ca-chain" in n.lower() and n.lower().endswith(wanted_suffixes):
                return n, zf_or_dir.read(n)
        # XAPK / app bundle: look inside nested APKs too.
        for n in names:
            if not n.lower().endswith(".apk"):
                continue
            try:
                inner = zipfile.ZipFile(io.BytesIO(zf_or_dir.read(n)))
            except Exception:
                continue
            inner_names = [x for x, _ in zip_bytes_names(inner)]
            for want in ("assets/ca-chain.crt", "ca-chain.crt"):
                for x in inner_names:
                    if x.lower().endswith(want):
                        return f"{n}!{x}", inner.read(x)
    else:
        root = zf_or_dir
        for p in root.rglob("ca-chain*"):
            if p.is_file():
                return str(p), p.read_bytes()
    return None, None


# ------------------------------------------------------------- bks parsing --- #

def bks_certificates(data: bytes):
    """Certificates are stored *unencrypted* in a BKS, so they can be listed with
    no password at all. The layout is: ... u16 len, cert-type string, u32 certlen,
    DER certificate. We look for the type marker and read what follows, which is
    far more robust than scanning for any 0x30 0x82 sequence."""
    out = []
    for marker in (b"X.509", b"X509"):
        start = 0
        while True:
            i = data.find(marker, start)
            if i < 0:
                break
            start = i + 1
            p = i + len(marker)
            if p + 4 > len(data):
                continue
            ln = struct.unpack(">I", data[p:p + 4])[0]
            if not (16 < ln <= len(data) - p - 4):
                continue
            blob = data[p + 4:p + 4 + ln]
            if blob[:2] == b"\x30\x82" and len(blob) == ln:
                out.append(blob)
    # de-duplicate, keep order
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# --------------------------------------------------------------- passwords --- #

def candidate_passwords(apk_path, bks_name, extra):
    seen, out = set(), []

    def add(pw):
        if pw is not None and pw not in seen:
            seen.add(pw)
            out.append(pw)

    for pw in extra or []:
        add(pw)
    for pw in DEFAULT_PASSWORDS:
        add(pw)

    # Harvest plausible literals from the package: the keystore password very
    # often sits in the app's own resources/native libs as a plain constant.
    try:
        blobs = []
        if apk_path.is_file() and apk_path.suffix.lower() in (".apk", ".ipa", ".aab", ".zip", ".xapk"):
            with zipfile.ZipFile(apk_path) as zf:
                for n, sz in zip_bytes_names(zf):
                    if sz > 220 * 1024 * 1024:
                        continue
                    low = n.lower()
                    if low.endswith((".dex", ".so", ".json", ".xml", ".properties", ".bin")):
                        try:
                            blobs.append(zf.read(n))
                        except Exception:
                            pass
                    # XAPK / app bundle: also read the nested APKs' own entries
                    elif low.endswith(".apk") and sz <= 220 * 1024 * 1024:
                        try:
                            inner = zipfile.ZipFile(io.BytesIO(zf.read(n)))
                        except Exception:
                            continue
                        for x, xsz in zip_bytes_names(inner):
                            if xsz > 120 * 1024 * 1024:
                                continue
                            if x.lower().endswith((".dex", ".so", ".json", ".xml", ".properties", ".bin")):
                                try:
                                    blobs.append(inner.read(x))
                                except Exception:
                                    pass
        for b in blobs:
            for m in re.finditer(rb"[\x20-\x7e]{5,40}", b):
                s = m.group()
                # keep tokens that look like passwords: no path/class noise
                if any(c in s for c in b"/;:()[]{}<>$\"'\\ "):
                    continue
                if b"." in s and b"/" not in s and len(s) < 8:
                    continue
                if b"_" in s or b"-" in s:
                    continue
                try:
                    txt = s.decode("ascii")
                except UnicodeDecodeError:
                    continue
                if 5 <= len(txt) <= 24 and any(ch.isalpha() for ch in txt):
                    add(txt)
    except Exception:
        pass
    return out


def try_passwords(bks_bytes, passwords):
    """Return (password, store) for the first password that opens the keystore.

    pyjks wants a filename, so the keystore is staged in a private temp file.
    """
    import tempfile
    try:
        import jks
        import jks.bks  # noqa: F401
    except ImportError:
        raise SystemExit("pyjks is required:  pip install pyjks cryptography")
    tmpdir = tempfile.mkdtemp(prefix="bks-")
    tmp = os.path.join(tmpdir, "client.bks")
    with open(tmp, "wb") as fh:
        fh.write(bks_bytes)
    try:
        for pw in passwords:
            try:
                store = jks.bks.BksKeyStore.load(tmp, pw)
            except Exception:
                continue
            if store.entries:
                return pw, store
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None, None


def extract_key_and_chain(store, password):
    """Unwrap the sealed key entry. Returns (pkcs8_bytes, [cert_der, ...])."""
    import jks
    for entry in store.entries.values():
        if isinstance(entry, jks.bks.BksSealedKeyEntry):
            entry.decrypt(password)
            chain = getattr(entry, "cert_chain", []) or []
            nested = getattr(entry, "nested", None)
            if nested is not None:
                pk = getattr(nested, "pkey_pkcs8", None) or getattr(nested, "pkey", None)
                if pk:
                    return pk, [c.cert if hasattr(c, "cert") else c for c in chain]
        if isinstance(entry, jks.bks.BksKeyEntry):
            chain = getattr(entry, "cert_chain", []) or []
            pk = getattr(entry, "pkey_pkcs8", None) or getattr(entry, "pkey", None)
            if pk:
                return pk, [c.cert if hasattr(c, "cert") else c for c in chain]
    return None, []


# ------------------------------------------------------------------ verify --- #

def public_key_sha256_of_cert(cert_der: bytes):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import Encoding
        c = x509.load_der_x509_certificate(cert_der)
        der = c.public_key().public_bytes(
            Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        import hashlib
        return hashlib.sha256(der).hexdigest()
    except Exception:
        return None


def public_key_sha256_of_key(pkcs8: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        k = serialization.load_der_private_key(pkcs8, password=None)
        der = k.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        import hashlib
        return hashlib.sha256(der).hexdigest()
    except Exception:
        try:                                   # raw EC key (SEC1) fallback
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            k = serialization.load_der_ec_private_key(pkcs8)
            der = k.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            import hashlib
            return hashlib.sha256(der).hexdigest()
        except Exception:
            return None


def write_pem(path: pathlib.Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# -------------------------------------------------------------------- main --- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", required=True,
                    help="APK/IPA/zip, an extracted directory, or a .bks file")
    ap.add_argument("--out", default="./xpeng-certs", help="output directory")
    ap.add_argument("--password", action="append", default=[],
                    help="XPENG keystore password (repeatable); defaults are tried first")
    ap.add_argument("--list", action="store_true", help="inventory only, extract nothing")
    args = ap.parse_args()

    src = pathlib.Path(args.apk).expanduser()
    if not src.exists():
        print(f"ERROR: input not found: {src}")
        return 1
    out = pathlib.Path(args.out).expanduser()

    print(f"Source : {src}")
    print(f"Output : {out}\n")

    # 1. find the BKS
    name, bks = find_bks(src)
    if bks is None:
        print("No .bks keystore found in this input.")
        print("If this is an iOS .ipa, the app ships plain PEM instead - look for")
        print("  Payload/*.app/Frameworks/App.framework/certs/")
        return 2
    print(f"[1] keystore: {name}  ({len(bks)} bytes)")

    # 2. certificates are readable without a password
    certs = bks_certificates(bks)
    print(f"[2] certificate blobs found (unencrypted): {len(certs)}")
    for i, c in enumerate(certs[:3]):
        fp = public_key_sha256_of_cert(c)
        print(f"      [{i}] {len(c)} bytes  pubkey-sha256={fp}")

    if args.list:
        return 0

    # 3. unlock
    cands = candidate_passwords(src, name, args.password)
    print(f"[3] trying {len(cands)} candidate password(s)...")
    pw, store = try_passwords(bks, cands)
    if store is None:
        print("    no candidate password opened the XPENG keystore.")
        print("    XPENG 3.13.0 and later ship an ENCRYPTED keystore, so this")
        print("    APK-only route does not apply to them. If this is a supported")
        print("    build (3.7.0-3.12.0) the password is xiaopengapp; pass it with")
        print("    --password to skip the candidate scan.")
        return 2
    print(f"    PASSWORD FOUND: {pw!r}")

    # 4. pull the key
    print("[4] unwrapping private key entry...")
    pkcs8, chain = extract_key_and_chain(store, pw)
    if pkcs8 is None:
        print("    keystore opened but no private-key entry was found")
        return 2
    print(f"    key: {len(pkcs8)} bytes (PKCS#8); chain: {len(chain)} cert(s)")

    # 5. emit PEM
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat)
    from cryptography import x509

    try:
        key = serialization.load_der_private_key(pkcs8, password=None)
    except Exception as exc:
        print(f"    could not parse the key: {exc}")
        return 2
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                                NoEncryption())

    certs_pem = b""
    for der in chain:
        try:
            certs_pem += x509.load_der_x509_certificate(der).public_bytes(Encoding.PEM)
        except Exception:
            continue
    if not certs_pem and certs:
        certs_pem = x509.load_der_x509_certificate(certs[0]).public_bytes(Encoding.PEM)

    # CA chain
    ca_name, ca_bytes = (None, None)
    if src.is_file() and src.suffix.lower() in (".apk", ".aab", ".ipa", ".zip", ".xapk"):
        with zipfile.ZipFile(src) as zf:
            ca_name, ca_bytes = find_asset(zf, (".crt", ".pem"))
    else:
        ca_name, ca_bytes = find_asset(src, (".crt", ".pem"))
    if ca_bytes:
        print(f"[5] CA chain: {ca_name} ({len(ca_bytes)} bytes)")
        write_pem(out / CA_NAME, ca_bytes)
    else:
        print("[5] CA chain: not found in package (supply separately if needed)")

    write_pem(out / CERT_NAME, certs_pem)
    write_pem(out / KEY_NAME, key_pem)
    print(f"[6] wrote {out/CERT_NAME}")
    print(f"          {out/KEY_NAME}")

    # 6. verification: does the key actually match the certificate?
    kfp = public_key_sha256_of_key(pkcs8)
    cfp = None
    for der in chain:
        cfp = public_key_sha256_of_cert(der)
        if cfp:
            break
    if cfp is None and certs:
        cfp = public_key_sha256_of_cert(certs[0])
    print("\nVerification")
    print("------------")
    print(f"  key  public key sha256: {kfp}")
    print(f"  cert public key sha256: {cfp}")
    if kfp and cfp and kfp == cfp:
        print("  MATCH - the extracted private key is the key for this certificate.")
        return 0
    print("  WARNING: key/cert public keys differ - do not use this pair.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
