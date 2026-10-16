#!/usr/bin/env python3
"""Get the token+tenant pair a capture needs, out of the install that already has it.

The point of a companion tool is that it uses the **existing** connection rather
than asking for a second login. A working install already holds everything
required to mint MQTT credentials, in one of three places, and this module knows
all three:

1. **the config entry** - the import flow stores the pair on the entry itself
   (``data.login_tokens`` + ``data.tenant_data``), and that copy has no path
   dependency and cannot drift out of sync with a file on disk. This is the
   supported path and the one a capture should use.
2. **the legacy file options** - ``options.token_file`` / ``options.tenant_file``,
   for installs that predate the import flow or were built by
   ``tools/configure_ha_entry.py``. Read only when the entry carries no pair.
3. **cached MQTT credentials** - ``data.client_id`` / ``data.password`` /
   ``data.server``, the last account the integration minted. These expire, so
   they are a fallback for "the pair is dead but the session is still alive",
   never the first choice.

Reading ``.storage/core.config_entries`` directly is deliberate: the pair is not
exposed over the REST or websocket API, and the alternative - asking the operator
to paste live credentials into a tool - is a worse trade than reading the file
the integration already owns. Nothing here imports Home Assistant, so the tool
runs on any machine that can see the file.

**Credentials never appear in any output of this module.** ``describe()`` returns
identifiers only: a VIN, a uid, a device id prefix, the broker host.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

#: Where Home Assistant keeps config entry data. The same path inside the HA
#: container, which is where a capture run on the HA host reads it from.
DEFAULT_STORAGE = "/config/.storage/core.config_entries"
DEFAULT_CONFIG_DIR = "/config"

#: The renewal app's ``pair_dir``: a live token+tenant pair on disk that the
#: app rewrites on every successful renewal. Worth preferring over the entry's
#: own copy when both exist, because a capture is often started right after a
#: renewal - and this pair is the one that was just proven to mint.
DEFAULT_PAIR_DIR = "/config/xpeng-secrets"
PAIR_TOKENS_FILE = "xpeng_tokens.json"
PAIR_TENANT_FILE = "xpeng_tenant.json"

#: Entry data / option keys, mirroring custom_components/xpeng/const.py. Repeated
#: rather than imported so this module stays stdlib-only and loadable without the
#: integration (and therefore without ``cryptography``) installed.
KEY_LOGIN_TOKENS = "login_tokens"
KEY_TENANT_DATA = "tenant_data"
KEY_CLIENT_ID = "client_id"
KEY_PASSWORD = "password"
KEY_SERVER = "server"
KEY_VIN = "vin"
OPT_CONFIGURED_DEVICE_ID = "configured_device_id"
OPT_TOKEN_FILE = "token_file"
OPT_TENANT_FILE = "tenant_file"


class CredentialError(RuntimeError):
    """No usable pair could be resolved, with the reason a person can act on."""


@dataclass
class CredentialPair:
    """Everything a capture needs, and nothing secret in the repr."""

    tokens: dict[str, Any] = field(default_factory=dict)
    tenant: dict[str, Any] = field(default_factory=dict)
    device_id: str = ""
    vin: str = ""
    uid: str = ""
    origin: str = ""
    entry_id: str = ""
    cached_client_id: str = ""
    cached_password: str = ""
    cached_server: str = ""

    @property
    def can_mint(self) -> bool:
        """True when ``createMqttAccount`` can be called with this pair."""
        return bool(self.tokens.get("accessToken")) and bool(self.tenant.get("keys"))

    @property
    def user_topic_suffix(self) -> str:
        return self.uid or "unknown"

    def describe(self) -> dict[str, str]:
        """A redacted summary, safe to print and to put in a capture's metadata."""
        return {
            "origin": self.origin,
            "entry_id": self.entry_id,
            "vin": self.vin,
            "uid": self.uid,
            "device_id": _prefix(self.device_id),
            "server": self.cached_server,
            "account": str(self.tokens.get("account_email") or ""),
        }

    def auth_probe(self) -> str:
        """Short fingerprint of the credentials, so two runs can be compared.

        The tail of the access token, not the head: a token's prefix is fixed
        across mints, so a prefix tells you nothing about whether it changed.
        """
        token = str(self.tokens.get("accessToken") or "")
        return f"…{token[-6:]}" if len(token) > 6 else "(none)"


def _prefix(value: str, keep: int = 8) -> str:
    value = value or ""
    return f"{value[:keep]}…" if len(value) > keep else value


# -- storage ---------------------------------------------------------------- #


def load_storage(path: str = DEFAULT_STORAGE) -> dict[str, Any]:
    """Read a Home Assistant ``.storage`` JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise CredentialError(
            f"{path} does not exist. On an HAOS install the path is "
            f"{DEFAULT_STORAGE}; on Container/Core it is inside your config "
            "directory. Pass --entry-file, or use --tokens/--tenant instead."
        ) from None
    except OSError as err:
        raise CredentialError(f"{path}: cannot be read ({err.strerror})") from err
    except ValueError as err:
        raise CredentialError(f"{path}: not valid JSON ({err})") from err


def entries_from_storage(store: dict[str, Any], domain: str = "xpeng") -> list[dict[str, Any]]:
    """The XPENG config entries in a parsed ``core.config_entries``."""
    entries = ((store or {}).get("data") or {}).get("entries")
    if not isinstance(entries, list):
        raise CredentialError(
            "that file has no data.entries list - it is not core.config_entries"
        )
    return [e for e in entries if isinstance(e, dict) and e.get("domain") == domain]


def select_entry(
    entries: list[dict[str, Any]],
    *,
    entry_id: str = "",
    vin: str = "",
) -> dict[str, Any]:
    """Pick one entry, refusing to guess when the choice is ambiguous."""
    if not entries:
        raise CredentialError(
            "no XPENG entry found in that storage file - is the integration "
            "configured on this Home Assistant?"
        )
    if entry_id:
        for entry in entries:
            if entry.get("entry_id") == entry_id:
                return entry
        raise CredentialError(
            f"no entry with id {entry_id}; present: "
            + ", ".join(str(e.get('entry_id')) for e in entries)
        )
    if vin:
        wanted = vin.strip().upper()
        for entry in entries:
            if str(entry.get("unique_id") or "").upper() == wanted:
                return entry
            if str((entry.get("data") or {}).get(KEY_VIN) or "").upper() == wanted:
                return entry
        raise CredentialError(f"no entry for VIN {wanted}")

    if len(entries) > 1:
        listed = ", ".join(
            f"{e.get('entry_id')} ({e.get('title')})" for e in entries
        )
        raise CredentialError(
            f"this Home Assistant has {len(entries)} XPENG entries ({listed}); "
            "pass --entry-id (or --vin) to say which car to capture"
        )
    return entries[0]


# -- pair extraction -------------------------------------------------------- #


def _tokens_from_wrapper(raw: Any) -> dict[str, Any]:
    """Accept both token-file shapes the onboarding tooling has written.

    ``onboard_email_login.py`` writes ``{"json": {...}}``; other paths write the
    bare object. Same unwrapping as ``config_flow._tokens_from_text``, kept here
    so this module does not have to import Home Assistant.
    """
    if isinstance(raw, str):
        start = raw.find("{")
        if start < 0:
            raise CredentialError("the token file contains no JSON object")
        try:
            raw = json.loads(raw[start:])
        except ValueError as err:
            raise CredentialError(f"the token file is not valid JSON ({err})") from err
    if not isinstance(raw, dict):
        raise CredentialError("the token file is not a JSON object")
    inner = raw.get("json")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str) and inner.lstrip().startswith("{"):
        return _tokens_from_wrapper(inner)
    text = raw.get("jsonText")
    if isinstance(text, str) and "{" in text:
        return _tokens_from_wrapper(text)
    return raw


def _tenant_from_wrapper(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        start = raw.find("{")
        if start < 0:
            raise CredentialError("the tenant file contains no JSON object")
        try:
            raw = json.loads(raw[start:])
        except ValueError as err:
            raise CredentialError(f"the tenant file is not valid JSON ({err})") from err
    if not isinstance(raw, dict):
        raise CredentialError("the tenant file is not a JSON object")
    return raw


def pair_from_entry(
    entry: dict[str, Any],
    *,
    read_text: Callable[[str], str] | None = None,
    config_dir: str = DEFAULT_CONFIG_DIR,
) -> CredentialPair:
    """Resolve the pair an entry carries, or points at.

    Order matters and is the same order the integration uses: the entry's own
    pair wins over the file options, because a pair on the entry cannot drift.
    """
    read_text = read_text or _read_text
    data = entry.get("data") or {}
    options = entry.get("options") or {}

    tokens: dict[str, Any] = {}
    tenant: dict[str, Any] = {}
    origin = ""

    stored_tokens = data.get(KEY_LOGIN_TOKENS)
    stored_tenant = data.get(KEY_TENANT_DATA)
    if isinstance(stored_tokens, dict) and stored_tokens:
        tokens = dict(stored_tokens)
        origin = f"config entry {entry.get('entry_id')} (login_tokens)"
    if isinstance(stored_tenant, dict) and stored_tenant.get("keys"):
        tenant = dict(stored_tenant)

    if not tokens:
        path = _resolve_path(options.get(OPT_TOKEN_FILE) or "", config_dir)
        if path:
            tokens = _tokens_from_wrapper(json.loads(read_text(path)))
            origin = f"token file {path}"

    if not tenant:
        path = _resolve_path(options.get(OPT_TENANT_FILE) or "", config_dir)
        if path:
            tenant = _tenant_from_wrapper(json.loads(read_text(path)))
            if not origin:
                origin = f"tenant file {path}"

    device_id = str(options.get(OPT_CONFIGURED_DEVICE_ID) or tenant.get("deviceId") or "")
    tenant_device_id = str(tenant.get("deviceId") or "")
    if tenant_device_id and device_id and tenant_device_id != device_id:
        # The 12010104 trap: the key pool is sealed with md5(deviceId), the token
        # pair is bound to the id the tenant records, and minting against a
        # different one fails on every call. Refuse rather than produce a capture
        # that silently receives nothing.
        raise CredentialError(
            "this entry is configured with device id "
            f"{_prefix(device_id)} but its tenant records {_prefix(tenant_device_id)}. "
            "Every API call will fail 12010104 until the install is re-onboarded; "
            "capturing against it would record an empty file and call it silence."
        )

    vin = str(data.get(KEY_VIN) or entry.get("unique_id") or "").strip().upper()
    pair = CredentialPair(
        tokens=tokens,
        tenant=tenant,
        device_id=device_id or tenant_device_id,
        vin=vin,
        uid=str(tokens.get("uid") or ""),
        origin=origin or f"config entry {entry.get('entry_id')}",
        entry_id=str(entry.get("entry_id") or ""),
        cached_client_id=str(data.get(KEY_CLIENT_ID) or ""),
        cached_password=str(data.get(KEY_PASSWORD) or ""),
        cached_server=str(data.get(KEY_SERVER) or ""),
    )
    if not pair.uid and pair.cached_client_id:
        pair.uid = _uid_from_client_id(pair.cached_client_id)
    return pair


def pair_dir_present(pair_dir: str) -> bool:
    """Whether ``pair_dir`` holds a complete pair, without reading either file."""
    if not pair_dir:
        return False
    return all(
        os.path.isfile(os.path.join(pair_dir, name))
        for name in (PAIR_TOKENS_FILE, PAIR_TENANT_FILE)
    )


def pair_from_pair_dir(pair_dir: str) -> CredentialPair:
    """The pair the renewal app wrote, which is the inter-app hand-off."""
    pair = pair_from_files(
        os.path.join(pair_dir, PAIR_TOKENS_FILE),
        os.path.join(pair_dir, PAIR_TENANT_FILE),
    )
    pair.origin = f"pair_dir {pair_dir}"
    return pair


def pair_from_files(tokens_path: str, tenant_path: str) -> CredentialPair:
    """The ``--tokens``/``--tenant`` path, for a machine that has no HA storage."""
    tokens = _tokens_from_wrapper(json.loads(_read_text(tokens_path)))
    tenant = _tenant_from_wrapper(json.loads(_read_text(tenant_path)))
    return CredentialPair(
        tokens=tokens,
        tenant=tenant,
        device_id=str(tenant.get("deviceId") or ""),
        vin=str(tokens.get("vin") or "").strip().upper(),
        uid=str(tokens.get("uid") or ""),
        origin=f"{tokens_path} + {tenant_path}",
    )


def _uid_from_client_id(client_id: str) -> str:
    """``xmart:app:<uid>:<deviceId>`` -> ``<uid>``."""
    parts = client_id.split(":")
    return parts[2] if len(parts) >= 3 else ""


def _resolve_path(path: str, config_dir: str) -> str:
    """Entry paths are container-absolute; a run outside the container may not be."""
    path = (path or "").strip()
    if not path:
        return ""
    if os.path.exists(path):
        return path
    if path.startswith("/config/"):
        local = os.path.join(config_dir.rstrip("/"), path[len("/config/") :])
        if os.path.exists(local):
            return local
    return path  # let the caller's open() produce the error, with the real path


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        raise CredentialError(f"{path} does not exist") from None
    except OSError as err:
        raise CredentialError(f"{path}: cannot be read ({err.strerror})") from err


__all__ = [
    "CredentialError",
    "CredentialPair",
    "DEFAULT_PAIR_DIR",
    "DEFAULT_STORAGE",
    "entries_from_storage",
    "load_storage",
    "pair_dir_present",
    "pair_from_entry",
    "pair_from_files",
    "pair_from_pair_dir",
    "select_entry",
]
