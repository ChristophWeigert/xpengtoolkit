#!/usr/bin/env python3
"""Record the XPENG broker's raw traffic to a file, outside Home Assistant.

Why this is a companion tool and not an integration feature
-----------------------------------------------------------
``mqtt_listener_enabled`` + ``mqtt_topic_discovery`` already subscribe inside
Home Assistant, and against ``#`` on this broker that means the whole broker -
including the car's 1 Hz CAN broadcast. Doing the expensive work per frame (an
HA event, a full coordinator update that re-renders every entity, and the
recorder rows that follow) wedged a user's Home Assistant on 0.4.30. 0.4.32
coalesces that work to one update per second, which fixes the wedge - but
coalescing is the wrong answer for *recording*, because a recording whose
per-frame timing has been smoothed away cannot answer the question it exists to
answer.

This tool keeps the firehose out of Core entirely:

* it mints its own MQTT session from the credentials the install already holds
  (see ``xpeng_ha_credentials``), so there is no second login and no new secret;
* it subscribes to the same topics the integration's discovery tries;
* it writes every frame verbatim to a binary journal (``xpeng_can_journal``);
* it does no decoding at all. Decoding happens offline, against the file, with
  ``xpeng_can_decode.py`` - so a decoder can be rewritten without re-capturing,
  and a bad decoder cannot cost you the capture.

Coexisting with the integration
-------------------------------
XPENG authorises **one MQTT session per account**. Three places in this
repository say so, and they are consistent with each other: the guide's "one
active session per account", the fault-finder's "two MQTT clients for the same
account ... the broker authorises one and refuses the other", and ``probe_seat``'s
note that a second session evicts the first.

That makes the client id the whole question, and there are two ways to present it:

``--client-id-mode unique`` **(default)** mints the credentials and then connects
under a *different* client id, derived from the minted one. The integration keeps
its session either way: the broker can accept this one (proving two sessions on
one account really are allowed here) or refuse it as ``Not authorized`` - and a
refusal costs you a captured nothing, not a wedged Home Assistant. This is the
mode to probe with, and the mode to run under HA.

``--client-id-mode shared`` presents the minted id unchanged, which is the
integration's own identity. That *will* evict Home Assistant's session and let its
watchdog reconnect underneath you, so it needs ``--confirm-session-takeover``.
Use it only with the integration disabled - or not at all.

If ``unique`` is refused, this account cannot carry a second MQTT session and no
client id will change that. The two ways on are a second XPENG account with the
car shared to it (the same thing this integration already asks of its own users),
or capturing from inside the integration's existing connection, where there is no
second session to authorise.

Nothing in this tool publishes. It never wakes the car. (``--force-upload``
opts into the one publish that does, and is off by default.)

Usage
-----
    # what would it do? reads credentials, no network, no paho needed
    xpeng_can_capture.py --dry-run

    # 60 seconds under a distinct client id: coexists, or is refused, and either
    # way the integration keeps its session
    xpeng_can_capture.py --duration 60

    # five minutes of the car's CAN topics, written to ./xpeng-can-<utc>.xpgcan
    xpeng_can_capture.py --duration 300

    # the whole broker, which is a lot of data - expect the byte cap to be the
    # thing that ends this, not the clock
    xpeng_can_capture.py --wildcard '#' --max-mb 256

Requirements: Python 3, ``cryptography`` and ``paho-mqtt``, plus the three
certificate files the integration uses (``--cert-dir``, or the install's own).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

# tools/ is not a package; both helpers are loaded as siblings.
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import xpeng_can_journal as journal  # noqa: E402
import xpeng_ha_credentials as creds  # noqa: E402

COMPONENT_DIR = TOOLS_DIR.parent / "custom_components" / "xpeng"
TOOL_VERSION = "1.0"

#: How long after a connect attempt a missing connection counts as a failure.
#: paho reports is_connected() == False until the CONNACK arrives, so checking it
#: the instant after connect() turns a merely slow acknowledgement into a
#: reconnect loop - and each reconnect mints a credential and evicts the session
#: it just made.
CONNECT_GRACE_S = 10.0

#: MQTT reason codes this tool treats as "the account already has a session".
#: 5 is CONNACK "Not authorized"; 142 is DISCONNECT "Session taken over".
NOT_AUTHORIZED = frozenset({4, 5, 134, 135})
SESSION_TAKEN_OVER = 142

#: The broker topics worth trying, mirroring ``MQTT_DISCOVERY_CANDIDATES`` in
#: ``custom_components/xpeng/const.py``. Kept in step by hand because const.py
#: lives in the integration and this is a standalone tool; if that tuple grows,
#: this list should grow with it.
CAN_CANDIDATES: tuple[str, ...] = (
    "Xmart/Car/+/State/CanPack1s",
    "Xmart/Tbox/+/CanPack1s",
    "Xmart/TboxD21B/+/CanPack1s",
    "Xmart/Tbox/+/StatPack",
    "Xmart/Car/+/CanPackV2/Repub",
)

#: Only a capture should ever see these: they are EMQX shared subscriptions, so
#: subscribing takes messages *away* from whoever else is consuming the group -
#: potentially the car's own consumers. Never a default.
SHARED_SUBSCRIPTION_NOTE = (
    "$local/$queue/... and $share/... are deliberately not in the default set: "
    "they are shared subscriptions, so subscribing pulls messages away from the "
    "other consumers of that group rather than copying them."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record raw XPENG MQTT frames to a binary journal for offline decoding."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credential sources, in order of preference: the XPENG config entry "
            "(--entry-file), then --tokens/--tenant, then the entry's cached MQTT "
            "account (--use-cached-mqtt).\n\n" + SHARED_SUBSCRIPTION_NOTE
        ),
    )

    src = parser.add_argument_group("credentials")
    src.add_argument(
        "--entry-file",
        default=creds.DEFAULT_STORAGE,
        help=(
            "Home Assistant .storage/core.config_entries to read the pair from "
            f"(default: {creds.DEFAULT_STORAGE})"
        ),
    )
    src.add_argument("--entry-id", default="", help="capture a specific config entry")
    src.add_argument("--vin", default="", help="pick the entry by VIN")
    src.add_argument(
        "--config-dir",
        default=creds.DEFAULT_CONFIG_DIR,
        help=(
            "where /config maps to on this machine, so an entry's token/tenant "
            f"file paths resolve outside the container (default: {creds.DEFAULT_CONFIG_DIR})"
        ),
    )
    src.add_argument(
        "--pair-dir",
        default=creds.DEFAULT_PAIR_DIR,
        help=(
            "directory holding the renewal app's xpeng_tokens.json and "
            "xpeng_tenant.json. Preferred over the entry when both files are "
            f"present, because that pair is the one just renewed (default: {creds.DEFAULT_PAIR_DIR})"
        ),
    )
    src.add_argument("--no-pair-dir", action="store_true", help="ignore --pair-dir and use the entry")
    src.add_argument("--tokens", default="", help="xpeng_tokens.json, instead of the entry")
    src.add_argument("--tenant", default="", help="xpeng_tenant.json, instead of the entry")
    src.add_argument(
        "--use-cached-mqtt",
        action="store_true",
        help=(
            "do not mint; use the client_id/password/server the entry last stored. "
            "Only for 'the pair is dead but the session still works'"
        ),
    )
    src.add_argument(
        "--client-id-mode",
        choices=("unique", "shared"),
        default="unique",
        help=(
            "unique (default) connects under a derived client id, so the "
            "integration cannot be evicted: the broker either accepts both "
            "sessions or refuses this one. shared reuses the minted id, which "
            "does evict the integration and needs --confirm-session-takeover"
        ),
    )
    src.add_argument(
        "--cert-dir",
        default="",
        help="directory holding ca-chain.crt, client-cert-0.pem and client-key.pem",
    )

    scope = parser.add_argument_group("what to record")
    scope.add_argument(
        "--wildcard",
        "-w",
        action="append",
        default=None,
        help=(
            "a topic to subscribe, repeatable. Accepts MQTT wildcards, including "
            "'#' for the entire broker"
        ),
    )
    scope.add_argument(
        "--vid",
        default="",
        help=(
            "the car's vid, to narrow the CAN candidates from 'Xmart/Car/+/...' to "
            "your own car. Learned from the summary if you do not know it yet"
        ),
    )
    scope.add_argument(
        "--no-can-candidates",
        action="store_true",
        help="subscribe only to --wildcard topics (or only your own user channel)",
    )
    scope.add_argument(
        "--force-upload",
        action="store_true",
        help=(
            "publish one cmd_type=2 force-upload to wake the car (the only publish "
            "this tool makes; off by default, because a capture should not disturb "
            "the car it is measuring)"
        ),
    )

    out = parser.add_argument_group("output")
    out.add_argument(
        "--out",
        default="",
        help="capture path (default: ./xpeng-can-<UTC>.xpgcan)",
    )
    out.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="seconds to record; 0 means until Ctrl+C (default: 300)",
    )
    out.add_argument(
        "--max-mb",
        type=float,
        default=2048.0,
        help="stop after this much capture data in total (default: 2048)",
    )
    out.add_argument(
        "--rotate-mb",
        type=float,
        default=256.0,
        help="roll to a new part after this much in one file; 0 disables (default: 256)",
    )
    out.add_argument("--quiet", action="store_true", help="only warnings and the final summary")

    parser.add_argument(
        "--confirm-session-takeover",
        action="store_true",
        help=(
            "acknowledge that this minted clientId is the same identity the "
            "integration uses, so the broker will evict Home Assistant's XPENG "
            "session while the capture runs"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve credentials and print the plan, with no network and no capture",
    )
    return parser


# -- credential resolution -------------------------------------------------- #


def resolve_pair(args: argparse.Namespace) -> creds.CredentialPair:
    """The pair to use, in the order an install is most likely to have a live one.

    Explicit files, then the renewal app's ``pair_dir``, then the config entry.
    ``pair_dir`` sits above the entry because a capture is usually run either side
    of a renewal, and the pair there is the one that was just proven to mint -
    while the entry's copy is whatever the last import left. It is only used when
    both files are present, so an install that has never run the renewal app falls
    straight through to the entry.
    """
    if args.tokens or args.tenant:
        if not (args.tokens and args.tenant):
            raise creds.CredentialError(
                "--tokens and --tenant go together; one without the other is not a pair"
            )
        return creds.pair_from_files(args.tokens, args.tenant)

    if not args.no_pair_dir and creds.pair_dir_present(args.pair_dir):
        return creds.pair_from_pair_dir(args.pair_dir)

    store = creds.load_storage(args.entry_file)
    entries = creds.entries_from_storage(store)
    entry = creds.select_entry(entries, entry_id=args.entry_id, vin=args.vin)
    return creds.pair_from_entry(entry, config_dir=args.config_dir)


def plan_topics(pair: creds.CredentialPair, args: argparse.Namespace) -> list[str]:
    """The subscription list, own-car-first, with the wide ones named as wide."""
    topics: list[str] = []
    if args.wildcard:
        topics.extend(t.strip() for t in args.wildcard if t.strip())
    if not args.no_can_candidates:
        for candidate in CAN_CANDIDATES:
            if args.vid and candidate.startswith("Xmart/Car/+/"):
                candidate = candidate.replace("Xmart/Car/+", f"Xmart/Car/{args.vid}")
            topics.append(candidate)
    # The relayed user channel is the one topic we can always name exactly, and
    # it is where app-tier credentials are documented to receive vehicle data.
    if pair.uid:
        for suffix in ("Xmart/Msg/User/", "Xmart/MsgV2/User/"):
            topics.append(f"{suffix}{pair.uid}")
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for topic in topics:
        if topic not in seen:
            seen.add(topic)
            unique.append(topic)
    return unique


def wide_topics(topics: list[str]) -> list[str]:
    """Subscriptions whose reach is not obviously one car."""
    return [t for t in topics if "#" in t or "+" in t]


# -- recorder --------------------------------------------------------------- #


class Recorder:
    """Owns the MQTT client, the journal, credential rotation and the budgets."""

    def __init__(self, pair: creds.CredentialPair, args: argparse.Namespace, topics: list[str]):
        self.pair = pair
        self.args = args
        self.topics = topics
        self.stop_reason = ""

        self._api = None
        self._mqtt = None
        self._client = None
        self._creds: dict = {}
        self._expires_at = 0.0
        self._mid_topics: dict[int, list[str]] = {}

        # on_message runs on paho's network thread while run() may be rotating
        # the file underneath it; without this lock a frame can land in a journal
        # that has just been closed.
        self._lock = threading.Lock()
        self._last_connect_attempt = 0.0
        # Stable for the life of the run: a new suffix on every credential
        # rotation would mint a new session every 45s instead of keeping one.
        self._client_id_suffix = secrets.token_hex(4)
        self._auth_refusals = 0

        self.journal: journal.JournalWriter | None = None
        self._part = 1
        self._total_bytes = 0
        self._parts: list[str] = []

        self.messages = 0
        self.payload_bytes = 0
        self._last_ns = 0
        self.max_gap_ns = 0
        self.gap_topic = ""
        self.stopping = False

    # -- lifecycle ---------------------------------------------------------- #

    def _load_stack(self) -> None:
        import importlib.util

        import paho.mqtt.client as mqtt  # noqa: F401

        self._mqtt = mqtt

        def load(name: str, filename: str):
            path = COMPONENT_DIR / filename
            if not path.is_file():
                raise SystemExit(f"{filename} not found in {COMPONENT_DIR}")
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module

        # mqtt_client first: it is where build_tls_context lives, and it imports
        # xpeng_api itself when loaded as a top-level module.
        self._mqtt_client_mod = load("xpeng_mqtt_client", "mqtt_client.py")
        self._api = load("xpeng_api", "xpeng_api.py")

    def _cert_dir(self) -> str:
        if self.args.cert_dir:
            return self.args.cert_dir
        return self._api.resolve_cert_dir("")

    def _mint(self) -> dict:
        cfg = dict(self._api.DEFAULT_APP_CONFIG)
        cfg["ConfiguredDeviceId"] = self.pair.device_id
        tenant = self._api.deserialize_tenant(self.pair.tenant)
        return self._api.create_mqtt_account(cfg, tenant, self.pair.tokens)

    def _cached_credentials(self) -> dict:
        cached = {
            "clientId": self.pair.cached_client_id,
            "password": self.pair.cached_password,
            "serverSsl": self.pair.cached_server,
        }
        if not cached["clientId"] or not cached["password"]:
            raise creds.CredentialError(
                "the entry stores no usable cached MQTT account; drop "
                "--use-cached-mqtt so a fresh one is minted"
            )
        return cached

    def _fresh_credentials(self) -> dict:
        if self.args.use_cached_mqtt:
            return self._cached_credentials()
        account = self._mint()
        # Keep the cached copy current: it is what --use-cached-mqtt would use,
        # and what makes a failed mint diagnosable afterwards.
        self.pair.cached_client_id = account.get("clientId", self.pair.cached_client_id)
        self.pair.cached_password = account.get("password", "")
        self.pair.cached_server = account.get("serverSsl", self.pair.cached_server)
        if not self.pair.uid:
            parts = str(account.get("clientId", "")).split(":")
            if len(parts) >= 3:
                self.pair.uid = parts[2]
        return account

    def start(self) -> None:
        self._load_stack()
        boot = {
            "tool": "xpeng_can_capture",
            "tool_version": TOOL_VERSION,
            "identity": self.pair.describe(),
            "subscribed": self.topics,
            "wide_topics": wide_topics(self.topics),
            "cert_dir": self._cert_dir(),
            "credentials": (
                "cached config-entry account" if self.args.use_cached_mqtt else "minted via createMqttAccount"
            ),
            "client_id_shared_with_integration": True,
        }
        self._open_journal(boot)
        self._connect(initial=True)

    # -- journal ------------------------------------------------------------ #

    def _journal_path(self, part: int) -> str:
        base = self.args.out
        if part == 1:
            return base
        stem, ext = os.path.splitext(base)
        return f"{stem}-part{part}{ext}"

    def _open_journal(self, meta: dict, *, locked: bool = False) -> None:
        path = self._journal_path(self._part)
        writer = journal.JournalWriter(
            path,
            {**meta, "part": self._part, "opened_utc": journal.utc_now()},
        )
        if locked:
            self.journal = writer
        else:
            with self._lock:
                self.journal = writer
        self._parts.append(path)
        if not self.args.quiet:
            print(f"  capture: {path}")

    def _roll_if_needed(self) -> None:
        if not self.journal or not self.args.rotate_mb:
            return
        limit = int(self.args.rotate_mb * 1024 * 1024)
        with self._lock:
            if self.journal is None or self.journal.payload_bytes < limit:
                return
            self._roll_locked()

    def _roll_locked(self) -> None:
        """Roll parts. Caller holds the lock."""
        previous = self._parts[-1]
        self.journal.close(reason="rotated", messages=self.messages)
        self._part += 1
        self._open_journal(
            {
                "tool": "xpeng_can_capture",
                "tool_version": TOOL_VERSION,
                "identity": self.pair.describe(),
                "subscribed": self.topics,
                "continued_from": previous,
            },
            locked=True,
        )
        self.journal.note("rotation", part=self._part)

    # -- mqtt --------------------------------------------------------------- #

    def _connect(self, *, initial: bool = False) -> None:
        mqtt = self._mqtt
        while True:
            try:
                self._creds = self._fresh_credentials()
            except Exception as err:  # noqa: BLE001 - reported, then retried
                self._note("mint_failed", error=f"{type(err).__name__}: {err}")
                if initial:
                    raise
                print(f"  [!] could not mint credentials: {err}", file=sys.stderr)
                time.sleep(10)
                continue
            break

        host, _, port = str(self._creds.get("serverSsl", "")).partition(":")
        if not host:
            raise SystemExit(f"the API returned no usable broker: {self._creds.get('serverSsl')!r}")
        expire_in = self._creds.get("expireInSec")
        # Rotate with a margin, for the same reason the integration does: a
        # reconnect that presents a retired password is refused, and looks like
        # an auth failure rather than a rotation.
        margin = 20.0 if not expire_in else max(15.0, min(60.0, float(expire_in) * 0.25))
        self._expires_at = time.monotonic() + max(30.0, float(expire_in or 600) - margin)

        client_id, username = self._connection_identity()
        client = mqtt.Client(
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            clean_session=True,
        )
        client.username_pw_set(username, self._creds["password"])
        client.tls_set_context(self._mqtt_client_mod.build_tls_context(self._cert_dir()))
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        # Our own loop reconnects with fresh credentials; paho's would retry with
        # the retired password it was built with.
        client.reconnect_delay_set(min_delay=1, max_delay=5)

        self._client = client
        self._last_connect_attempt = time.monotonic()
        client.connect(host, int(port or 8885), keepalive=60)
        client.loop_start()
        if not self.args.quiet:
            print(f"  broker : {host}:{port or 8885} | rotate in {expire_in or '?'}s")

    def _connection_identity(self) -> tuple[str, str]:
        """The (clientId, username) this capture presents.

        ``unique`` derives a client id from the minted one, so the session this
        opens is a *different* one from the integration's. That is worth doing
        even though the account rule may refuse it anyway: a refusal is recorded
        and costs a capture, where presenting the integration's own id would evict
        a working Home Assistant instead.

        The username stays the minted identity in both modes, because that is what
        the broker authorises against - changing it would only turn "the account
        is busy" into "the credentials are wrong", which is a worse answer.
        """
        minted = self._creds["clientId"]
        username = self._creds.get("username") or minted
        if self.args.client_id_mode == "shared":
            # Exactly what the integration presents, including its choice to use
            # the client id as the username.
            return minted, minted
        return f"{minted}-cap{self._client_id_suffix}", username

    def _teardown_client(self) -> None:
        if not self._client:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not mask the real exit
            pass
        self._client = None

    def _reconnect(self, why: str) -> None:
        self._note("reconnect", why=why)
        self._teardown_client()
        self._connect()

    def _note(self, event: str, **fields) -> None:
        with self._lock:
            if self.journal and not self.journal.closed:
                try:
                    self.journal.note(event, **fields)
                except journal.JournalError:
                    pass
        if event in ("disconnect", "mint_failed", "reconnect", "subscribe_refused"):
            print(f"  [!] {event}: {json.dumps(fields, ensure_ascii=False)[:200]}", file=sys.stderr)

    # -- callbacks ---------------------------------------------------------- #

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        code = int(getattr(reason_code, "value", reason_code))
        self._note("connect", rc=code, broker=self._creds.get("serverSsl", ""))
        if code != 0:
            if code in NOT_AUTHORIZED:
                self._auth_refusals += 1
                self._note(
                    "account_session_limited",
                    rc=code,
                    attempts=self._auth_refusals,
                    detail=(
                        "the broker refused this session. XPENG authorises one MQTT "
                        "session per account, so this is the account being busy "
                        "rather than a bad credential - the integration still holds "
                        "the session. A client id cannot change that; a second "
                        "XPENG account with the car shared to it can."
                    ),
                )
                if self._auth_refusals >= 3:
                    self.stop_reason = "the account refused a second MQTT session"
                    self.stopping = True
            return
        self._auth_refusals = 0
        for topic in self.topics:
            result = client.subscribe(topic, qos=0)
            if result[0] == 0:
                self._mid_topics[result[1]] = [topic]
            else:
                self._note("subscribe_failed", topic=topic, rc=result[0])

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        topics = self._mid_topics.pop(mid, [])
        for index, topic in enumerate(topics):
            try:
                granted = int(getattr(reason_codes[index], "value", reason_codes[index]))
            except (IndexError, TypeError):
                granted = -1
            # 128 is the MQTT "not authorized" grant, and it is the answer worth
            # capturing: a topic the broker refuses looks exactly like silence.
            if granted >= 128 or granted < 0:
                self._note("subscribe_refused", topic=topic, granted=granted)
            else:
                self._note("subscribe_ok", topic=topic, granted=granted)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        code = int(getattr(reason_code, "value", reason_code))
        if self.stopping:
            return
        if code == SESSION_TAKEN_OVER:
            # Something else took the session over - the integration reconnecting,
            # or another client of this account. Worth naming, because it looks
            # like a network drop otherwise.
            self._note(
                "session_taken_over",
                rc=code,
                detail=(
                    "another client of this account claimed the session. If that "
                    "was Home Assistant reconnecting, the two are still fighting "
                    "and only one of them will ever hold it."
                ),
            )
            return
        self._note("disconnect", rc=code)

    def _on_message(self, client, userdata, msg):
        now_ns = time.time_ns()
        size = len(msg.payload)
        try:
            with self._lock:
                self.journal.message(
                    msg.topic,
                    msg.payload,
                    ts_ns=now_ns,
                    qos=int(msg.qos or 0),
                    retain=bool(getattr(msg, "retain", False)),
                )
        except journal.JournalError as err:
            self._note("journal_error", error=str(err))
            self.stop_reason = "journal error"
            self.stopping = True
            return

        if self._last_ns:
            gap = now_ns - self._last_ns
            if gap > self.max_gap_ns:
                self.max_gap_ns = gap
                self.gap_topic = msg.topic
        self._last_ns = now_ns

        self.messages += 1
        self.payload_bytes += size

    # -- main loop ---------------------------------------------------------- #

    def run(self) -> int:
        started = time.monotonic()
        max_bytes = int(self.args.max_mb * 1024 * 1024)
        last_status = 0.0
        try:
            while not self.stopping:
                time.sleep(0.25)
                now = time.monotonic()

                if self.args.duration and now - started >= self.args.duration:
                    self.stop_reason = "duration reached"
                    break
                if self.payload_bytes >= max_bytes:
                    self.stop_reason = f"byte cap reached ({self.args.max_mb} MB)"
                    break
                if now >= self._expires_at:
                    self._reconnect("credentials expiring")
                    continue
                if (
                    self._client is not None
                    and not self._client.is_connected()
                    and now - self._last_connect_attempt > CONNECT_GRACE_S
                ):
                    self._note("connection_lost", messages=self.messages)
                    self._reconnect("connection lost")
                    continue
                self._roll_if_needed()

                if not self.args.quiet and now - last_status >= 10:
                    last_status = now
                    elapsed = max(0.001, now - started)
                    print(
                        f"  {elapsed:6.1f}s  {self.messages:7d} msgs  "
                        f"{self.payload_bytes / 1e6:7.2f} MB  "
                        f"({self.payload_bytes / elapsed / 1000:6.1f} kB/s, "
                        f"{self.messages / elapsed:5.2f} msg/s)"
                    )
        except KeyboardInterrupt:
            self.stop_reason = "interrupted"
        finally:
            self.stopping = True
            self._teardown_client()
            if self.journal and not self.journal.closed:
                summary = self.journal.close(
                    reason=self.stop_reason or "stopped",
                    messages=self.messages,
                    payload_bytes=self.payload_bytes,
                    max_gap_s=round(self.max_gap_ns / 1e9, 3),
                    max_gap_topic=self.gap_topic,
                    parts=list(self._parts),
                )
                self._print_summary(summary)
        return 0

    def _print_summary(self, summary: dict) -> None:
        print()
        print(f"capture ended: {summary['reason']} after {summary['duration_s']}s")
        for index, path in enumerate(self._parts, start=1):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            print(f"  part {index}: {path} ({size / 1e6:.2f} MB)")
        print(f"  messages: {summary['messages']}   payload: {summary['payload_bytes'] / 1e6:.2f} MB")
        if self.max_gap_ns:
            print(
                f"  largest arrival gap: {self.max_gap_ns / 1e9:.2f}s "
                f"(on {self.gap_topic or 'unknown'})"
            )
        topics = summary.get("topics") or []
        if topics:
            print(f"  {len(topics)} topic(s):")
            for entry in sorted(topics, key=lambda t: -t["count"])[:15]:
                rate = entry.get("rate_hz")
                rate_s = f"{rate:.2f} Hz" if rate else "n/a"
                print(f"    {entry['count']:8d}  {rate_s:>10}  {entry['topic']}")
            if len(topics) > 15:
                print(f"    ... and {len(topics) - 15} more")
        else:
            print("  no messages. That is a result: see the notes in the capture, and")
            print("  run xpeng_can_decode.py notes on it for what the broker answered.")


# -- entry point ------------------------------------------------------------ #


def describe_plan(pair: creds.CredentialPair, topics: list[str], args: argparse.Namespace) -> str:
    lines = [
        "capture plan",
        "------------",
        f"  credentials : {pair.origin}",
        f"  identity    : VIN {pair.vin or '(unknown)'} | uid {pair.uid or '(unknown)'}",
        f"  device id   : {pair.describe()['device_id'] or '(none)'}",
        f"  can mint    : {'yes' if pair.can_mint else 'no (only --use-cached-mqtt can work)'}",
        f"  cached mqtt : {'present' if pair.cached_client_id else 'absent'}",
        *(
            [
                "  rotation    : none - --use-cached-mqtt reconnects with the same",
                "                stored password, so the capture will go quiet when",
                "                that account expires",
            ]
            if args.use_cached_mqtt
            else []
        ),
        f"  cert dir    : {args.cert_dir or 'the install default'}",
        f"  client id   : {args.client_id_mode}"
        + (" (derived; the integration keeps its session)"
           if args.client_id_mode == "unique"
           else " (the integration's own id - this evicts it)"),
        f"  output      : {args.out or '(auto)'}",
        f"  duration    : {args.duration}s" if args.duration else "  duration    : until interrupted",
        f"  byte caps   : {args.max_mb} MB total, {args.rotate_mb} MB per part",
        "",
        f"  {len(topics)} subscription(s):",
    ]
    lines.extend(f"    {topic}" for topic in topics)
    wide = wide_topics(topics)
    if wide:
        lines += [
            "",
            "  topics with a wildcard are broker-scoped, not car-scoped:",
        ]
        lines += [f"    {topic}" for topic in wide]
        lines += [
            "  If the broker does not already restrict those to your account, they",
            "  carry other vehicles' traffic too. The summary counts per car id, so",
            "  that is visible after the fact rather than assumed away. --vid",
            "  narrows the Xmart/Car/... ones to your own car.",
        ]
    return "\n".join(lines)


def takeover_refusal(args: argparse.Namespace) -> str:
    """The refusal to print before a capture that would evict the integration.

    Only ``--client-id-mode shared`` can do that, because only it presents the
    integration's own client id. Empty string means "carry on". Kept as a
    function so the rule can be tested without opening a socket.
    """
    if args.client_id_mode != "shared" or args.confirm_session_takeover:
        return ""
    return (
        "refusing --client-id-mode shared without --confirm-session-takeover.\n\n"
        "That mode presents the integration's own client id. The broker evicts\n"
        "the older session with that id, so this capture would drop Home\n"
        "Assistant's XPENG connection and its watchdog would reconnect while\n"
        "you are trying to measure the car's traffic.\n\n"
        "If you only want to find out whether this account allows a second\n"
        "session, do not pass this: the default (--client-id-mode unique) either\n"
        "coexists or is refused, and the integration keeps its session either\n"
        "way. If you do mean to take the session over, disable the integration\n"
        "(Settings -> Devices & services -> XPENG -> the entry -> Disable) for\n"
        "the length of the capture, then re-enable it."
    )


def default_out() -> str:
    return f"xpeng-can-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.xpgcan"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out = args.out or default_out()

    try:
        pair = resolve_pair(args)
    except creds.CredentialError as err:
        print(f"credentials: {err}", file=sys.stderr)
        return 2

    topics = plan_topics(pair, args)
    if not topics:
        print("nothing to subscribe to: pass --wildcard, or let the CAN candidates run", file=sys.stderr)
        return 2

    if args.dry_run:
        print(describe_plan(pair, topics, args))
        return 0

    refusal = takeover_refusal(args)
    if refusal:
        print(refusal, file=sys.stderr)
        return 3

    print("XPENG CAN capture")
    print(describe_plan(pair, topics, args))
    print()

    recorder = Recorder(pair, args, topics)

    def _stop(signum, _frame):
        recorder.stop_reason = f"signal {signum}"
        recorder.stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass  # not the main thread; Ctrl+C still raises KeyboardInterrupt

    try:
        recorder.start()
    except SystemExit:
        if recorder.journal and not recorder.journal.closed:
            recorder.journal.close(reason="start failed")
        raise
    except Exception as err:  # noqa: BLE001 - one clear line beats a traceback here
        print(f"could not start the capture: {type(err).__name__}: {err}", file=sys.stderr)
        if recorder.journal and not recorder.journal.closed:
            recorder.journal.close(reason="start failed", error=str(err))
        return 1

    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
