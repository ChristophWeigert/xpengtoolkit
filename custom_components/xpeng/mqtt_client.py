"""XPENG MQTT client - single persistent connection following bridge pattern."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Callable, Final

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

# Fallback directory for the mTLS material. Kept for callers that do not pass
# cert_dir; the integration passes the user-configured CONF_CERT_DIR instead.
CERT_DIR = Path(__file__).parent / "certs"

# Files required for mTLS, looked up inside the configured cert dir.
# ca-chain.crt is a public trust anchor; client-cert-0.pem and client-key.pem
# are app-level secrets the user must supply (never distributed with the
# project — see certs/README.md). The one list lives here and is imported by
# config_flow.py for its options-form validation.
CERT_FILES: Final = ("ca-chain.crt", "client-cert-0.pem", "client-key.pem")


def missing_cert_files(cert_dir: str | Path) -> list[Path]:
    """Return the required mTLS files that are missing from cert_dir.

    Paths are used as given (no ``~`` expansion) — absolute paths are expected,
    matching DEFAULT_CERT_DIR.
    """
    directory = Path(cert_dir)
    return [
        directory / name for name in CERT_FILES if not (directory / name).is_file()
    ]


def build_tls_context(cert_dir: str | Path) -> ssl.SSLContext:
    """Build the verifying mTLS context for the XPENG broker from cert_dir.

    Loads the client cert/key and the XPENG CA chain, then requires a verified
    peer (CERT_REQUIRED plus hostname checking) so the broker really is
    authenticated. The CA has to be loaded before verify_mode is set: with
    PROTOCOL_TLS_CLIENT verify_mode defaults to CERT_REQUIRED, so
    load_verify_locations() must run against that default rather than a
    verification-disabled context.

    Raises:
        FileNotFoundError: if any required file is missing, naming the missing
            file(s) and the directory searched.
    """
    directory = Path(cert_dir)
    missing = missing_cert_files(directory)
    if missing:
        raise FileNotFoundError(
            f"XPENG mTLS certificate file(s) missing from {directory}: "
            f"{', '.join(p.name for p in missing)}. "
            f"Required files: {', '.join(CERT_FILES)}. "
            "See custom_components/xpeng/certs/README.md for how to obtain "
            "them, or point the integration's 'Certificate directory' option "
            "at the directory holding them."
        )

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(
        str(directory / "client-cert-0.pem"),
        str(directory / "client-key.pem"),
    )
    ctx.load_verify_locations(cafile=str(directory / "ca-chain.crt"))
    # ca-chain.crt validates the broker's chain and the leaf CN matches the
    # host we dial, so demand a verified peer instead of accepting any cert.
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


def parse_listener_topics(raw: str) -> list[str]:
    """Parse newline/comma-separated MQTT topics from user input."""
    topics: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        topic = chunk.strip()
        if topic and len(topic) <= 512:
            topics.append(topic)
    return topics


class XPengMQTTClient:
    """Single persistent MQTT connection to the XPENG broker.

    Mirrors the working xpeng_mqtt_bridge.py pattern:
    - ONE client created at setup, paho loop_start() handles the network thread
    - paho handles auto-reconnect via reconnect_delay_set
    - Never create a new client on each poll
    """

    def __init__(
        self,
        client_id: str,
        password: str,
        server: str,
        user_id: str,
        cert_dir: str | Path | None = None,
    ) -> None:
        self._client_id = client_id
        self._password = password
        # The password the CURRENT paho client was built with. The credential
        # refresh replaces `_password` every ~45s while the live client keeps
        # authenticating with this one, so the two drift apart - and a refusal
        # then means "you used a superseded password", not "the login expired".
        self._client_password = ""
        self._server = server
        self._user_id = user_id
        # Where the mTLS material lives. Falls back to the bundled certs dir
        # for callers that predate the cert_dir option.
        self._cert_dir = Path(cert_dir) if cert_dir else CERT_DIR

        self._client: mqtt.Client | None = None
        self._connected = False
        self._msg_seq = 0
        self._last_msg_ts: float = time.time()
        # Time of the last message on the PRIMARY vehicle-state topic only.
        # Distinct from _last_msg_ts, which any topic bumps (V2, listener
        # topics). The silence watchdog must use THIS one: otherwise chatter on
        # a side topic keeps the watchdog quiet while the vehicle-state fan-out
        # is stalled - the exact failure the watchdog exists to fix.
        self._last_state_msg_ts: float = time.time()
        # Time of the last state message that actually CARRIED data. A sleeping
        # car's ~248-byte stub resets `_last_state_msg_ts` but not this, so a
        # stall that only produces stubs cannot hide behind the reconnect
        # cycle. This is the clock staleness/escalation is measured on.
        self._last_meaningful_state_ts: float = time.time()
        self._state_callback: Callable[[dict[str, Any]], None] | None = None
        self._auth_failure_callback: Callable[[], None] | None = None
        self._listener_callback: Callable[[str, str], None] | None = None
        self._subscription_result_callback: Callable[[str, bool, str], None] | None = None
        self._notification_callback: Callable[[dict[str, Any]], None] | None = None
        self._command_feedback_callback: Callable[[dict[str, Any]], None] | None = None
        self._last_full_content: dict[str, Any] | None = None

        # Listener state
        self._listener_enabled = False
        self._listener_topics: list[str] = []
        self._discovery_enabled = False
        self._subscription_mids: dict[int, str] = {}  # mid -> topic
        # Topics already announced at INFO, so a firehose logs one line each and no more.
        self._listener_logged_topics: set[str] = set()

        # Synchronous request/response correlation for send_command_wait().
        # Maps our outgoing msg_id -> {"event", "result", "service_type"}.
        self._pending_lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

        # Serialises the reconnect triggers (silence watchdog, auth-failure
        # callback, clientId rotation). Without this they stack and produce two
        # paho clients sharing one clientId that kick each other off the broker.
        self._reconnect_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        """Return True if currently connected."""
        return self._connected

    @property
    def last_msg_ts(self) -> float:
        """Unix timestamp of the most recent incoming MQTT message (any topic)."""
        return self._last_msg_ts

    @property
    def last_state_msg_ts(self) -> float:
        """Unix timestamp of the last message on the primary vehicle-state topic.

        Use this - not ``last_msg_ts`` - for "is the car's data flowing?"
        decisions such as the silence watchdog. A side topic (V2 or a user
        listener topic) bumps ``last_msg_ts`` without delivering vehicle state,
        which would mask a stalled state fan-out.
        """
        return self._last_state_msg_ts

    @property
    def last_meaningful_state_ts(self) -> float:
        """When the last vehicle-state message actually carried data.

        Distinct from ``last_state_msg_ts``, which any state-topic message bumps.
        A sleeping car's ~248-byte stub counts for that one but not for this, so
        this is the one to measure staleness with.
        """
        return self._last_meaningful_state_ts

    @property
    def credentials_superseded(self) -> bool:
        """Whether the live client is authenticating with an old password.

        `_password` is replaced every ~45s by the credential refresh; the paho
        client keeps the value it was built with. When those differ, a broker
        refusal means the client presented a superseded password - and the fix
        is to rebuild the client with the password already in hand, not to mint
        another one. Observed 2026-10-07: eight `rc=Not authorized` refusals in
        twenty minutes, each followed by a cooldown-delayed API round trip, when
        the password needed was sitting in memory the whole time.
        """
        return bool(self._client_password) and self._password != self._client_password

    @property
    def send_topic(self) -> str:
        """MQTT topic to publish vehicle queries."""
        return f"Xmart/Send/User/{self._user_id}"

    @property
    def recv_topic(self) -> str:
        """MQTT topic to subscribe for vehicle responses."""
        return f"Xmart/Msg/User/{self._user_id}"

    @property
    def recv_topic_v2(self) -> str:
        """V2 protocol variant — empirically reachable from app-class cert
        on iot-app-global (probed 2026-06-07, SUBACK Granted QoS 1).
        Currently unparsed; messages route to listener path for observation.
        """
        return f"Xmart/MsgV2/User/{self._user_id}"

    def set_state_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Set callback invoked from MQTT thread on full vehicle state."""
        self._state_callback = callback

    def set_auth_failure_callback(self, callback: Callable[[], None]) -> None:
        """Set callback invoked when MQTT broker rejects our credentials."""
        self._auth_failure_callback = callback

    def set_listener_callback(
        self, callback: Callable[[str, str], None]
    ) -> None:
        """Set callback for MQTT listener messages (topic, payload)."""
        self._listener_callback = callback

    def set_subscription_result_callback(
        self, callback: Callable[[str, bool, str], None]
    ) -> None:
        """Set callback for subscription results (topic, accepted, reason)."""
        self._subscription_result_callback = callback

    def set_notification_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Set callback for push notifications (msg_type=3)."""
        self._notification_callback = callback

    def set_command_feedback_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Set callback for command response messages (service_type != 19)."""
        self._command_feedback_callback = callback

    def configure_listener(
        self,
        enabled: bool,
        topics: list[str],
        discovery_enabled: bool,
        discovery_candidates: tuple[str, ...] = (),
    ) -> None:
        """Configure the MQTT listener settings.

        Called before connect() to set up listener topics.
        If discovery is enabled, the discovery candidates are appended.
        """
        self._listener_enabled = enabled
        self._listener_topics = list(topics)
        self._discovery_enabled = discovery_enabled
        if discovery_enabled:
            for candidate in discovery_candidates:
                if candidate not in self._listener_topics:
                    self._listener_topics.append(candidate)
        if enabled:
            _LOGGER.info(
                "MQTT listener configured: %d topics, discovery=%s",
                len(self._listener_topics),
                discovery_enabled,
            )
            # "#" subscribes to the entire broker, which on this one includes the car's
            # 1 Hz CAN broadcast (Xmart/Car/<vid>/State/CanPack1s) and every other
            # vehicle's traffic. It is allowed - it is the diagnostic's whole point - but
            # it is the setting that wedged a user's Home Assistant on 0.4.30, so say so
            # where the person who typed it will see it.
            if "#" in self._listener_topics:
                _LOGGER.warning(
                    "MQTT listener is subscribed to '#', which receives every message on "
                    "the broker, including the car's 1 Hz CAN stream. Diagnostic counters "
                    "and the rolling log stay exact, but expect heavy traffic; narrow the "
                    "topic list if Home Assistant feels slow."
                )

    def connect(self) -> None:
        """Create client, configure TLS, connect, start loop. Called ONCE."""
        if self._client is not None:
            self.disconnect()

        _LOGGER.info(
            "Creating XPENG MQTT client: server=%s user_id=%s",
            self._server,
            self._user_id,
        )

        client = mqtt.Client(
            client_id=self._client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            clean_session=True,
        )
        client.username_pw_set(self._client_id, self._password)
        self._client_password = self._password

        # mTLS setup — verifies the broker against ca-chain.crt.
        # build_tls_context() raises FileNotFoundError naming anything missing.
        client.tls_set_context(build_tls_context(self._cert_dir))

        # Callbacks
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe

        # Let paho handle reconnection
        # max_delay must stay BELOW the credential rotation interval
        # (mqtt_refresh_interval, 45s). paho retries with the password the
        # client was built with, so a retry scheduled further out than one
        # rotation always presents a retired password and is refused as
        # "Not authorized". _apply_current_credentials() re-syncs the password
        # before each attempt, which fixed the bulk of this; capping the backoff
        # closes the residual case where the retry is scheduled so far ahead
        # that a rotation lands in between (measured on the live install:
        # 16 refusals over 6h, ~9x better than the original 15-in-3h but not
        # zero). Cost: during a genuine broker outage paho reconnects every 30s
        # instead of every 60s. Each attempt is a cheap TCP connect that fails.
        client.reconnect_delay_set(min_delay=10, max_delay=30)

        # Parse host:port
        host, port_str = self._server.split(":")
        port = int(port_str)

        self._client = client
        client.connect(host, port, keepalive=60)
        client.loop_start()

        # Wait up to 10s for connection
        for _ in range(100):
            if self._connected:
                _LOGGER.info("XPENG MQTT connected successfully")
                return
            time.sleep(0.1)

        if not self._connected:
            _LOGGER.warning(
                "XPENG MQTT not connected after 10s, paho will keep retrying"
            )

    def reconnect(self, reason: str = "") -> bool:
        """Tear down and rebuild the client, serialised against other callers.

        The silence watchdog, the auth-failure callback and the clientId-
        rotation path all want to reconnect. If two run at once they create
        overlapping paho clients with the same clientId and the broker evicts
        one whenever the other connects, so the session never settles. This
        takes a non-blocking lock: if a reconnect is already running, the call
        is a no-op and returns False.
        """
        if not self._reconnect_lock.acquire(blocking=False):
            _LOGGER.info(
                "MQTT reconnect already in progress — skipping (%s)",
                reason or "unspecified",
            )
            return False
        try:
            _LOGGER.info("MQTT reconnect (%s)", reason or "unspecified")
            # connect() tears down any existing client first.
            self.connect()
            return True
        finally:
            self._reconnect_lock.release()

    def disconnect(self) -> None:
        """Disconnect and stop the network loop. Safe to call more than once.

        Order matters. paho sends the DISCONNECT packet from `disconnect()`, and
        `loop_stop()` stops the thread that would write it - so stopping the loop
        first can leave the broker holding the session. A session the broker
        still attributes to this clientId is exactly what makes a later
        reconnect look like a second subscriber under one account.

        `loop_stop()` joins the network thread, so it can block; everything
        before it here is therefore non-blocking, and the thread reference is
        dropped before the join so a hung join cannot leave this object claiming
        to own a live client.
        """
        client = self._client
        if client is None:
            return
        _LOGGER.info(
            "Disconnecting XPENG MQTT client (clientId=%s)", self._client_id
        )
        self._client = None
        self._connected = False
        self._subscription_mids.clear()
        # Detach callbacks first: if the thread outlives this call, it must not
        # keep feeding the coordinator, the watchdog or the auth-failure path as
        # a ghost of a client nobody owns any more.
        for attr in ("on_connect", "on_message", "on_disconnect", "on_subscribe"):
            try:
                setattr(client, attr, None)
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("MQTT disconnect() raised; continuing teardown", exc_info=True)
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("MQTT loop_stop() raised", exc_info=True)

    def _next_msg_id(self) -> str:
        """Build the next unique outgoing message id."""
        self._msg_seq = (self._msg_seq + 1) % 1000
        ts_ms = int(time.time() * 1000)
        return f"11{self._user_id}{ts_ms}{self._msg_seq:03d}"

    def _publish(
        self,
        vin: str,
        service_type: int,
        msg_content: dict,
        msg_type: int,
        msg_id: str,
    ) -> int:
        """Build and publish a command payload. Returns the paho rc."""
        payload = json.dumps({
            "target_id": vin,
            "msg_id": msg_id,
            "protocol_ver": "2.0",
            "msg_content": msg_content,
            "service_type": service_type,
            "msg_type": msg_type,
        })

        result = self._client.publish(self.send_topic, payload, qos=1)
        _LOGGER.info(
            "Sent command: service_type=%s msg_content=%s rc=%s msg_id=%s",
            service_type, msg_content, result.rc, msg_id,
        )
        return result.rc

    def send_command(
        self, vin: str, service_type: int, msg_content: dict, msg_type: int = 2
    ) -> bool:
        """Send a command to the vehicle via MQTT (fire-and-forget)."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot send command: not connected")
            return False
        rc = self._publish(vin, service_type, msg_content, msg_type, self._next_msg_id())
        return rc == 0

    def send_command_wait(
        self,
        vin: str,
        service_type: int,
        msg_content: dict,
        msg_type: int = 2,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Send a command/query and block until the matching response arrives.

        Correlates the reply by msg_id (exact) with a fallback to the next
        inbound message bearing the same service_type. Returns the parsed
        response dict, or None on timeout / not connected.

        MUST be called from an executor thread — it blocks on a
        threading.Event and would stall the event loop otherwise.
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot send command (wait): not connected")
            return None

        msg_id = self._next_msg_id()
        event = threading.Event()
        entry: dict[str, Any] = {
            "event": event,
            "result": None,
            "service_type": service_type,
        }
        # Register BEFORE publishing so a fast reply can't race ahead of us.
        with self._pending_lock:
            self._pending[msg_id] = entry
        try:
            rc = self._publish(vin, service_type, msg_content, msg_type, msg_id)
            if rc != 0:
                return None
            if event.wait(timeout):
                return entry["result"]
            _LOGGER.warning(
                "send_command_wait timed out after %ss (service_type=%s msg_id=%s)",
                timeout, service_type, msg_id,
            )
            return None
        finally:
            with self._pending_lock:
                self._pending.pop(msg_id, None)

    def _resolve_pending(self, data: dict[str, Any], stype: Any) -> None:
        """Wake any send_command_wait() waiter awaiting this inbound message."""
        with self._pending_lock:
            if not self._pending:
                return
            msg_id = data.get("msg_id")
            key: str | None = None
            if msg_id and msg_id in self._pending:
                key = msg_id
            else:
                # Fallback: first waiter expecting this service_type. The car
                # does not always echo our msg_id on state responses.
                for k, ent in self._pending.items():
                    if ent["service_type"] == stype:
                        key = k
                        break
            if key is None:
                return
            entry = self._pending[key]
            entry["result"] = data
            entry["event"].set()

    def send_query(self, vin: str) -> None:
        """Publish a vehicle state query."""
        if not self._client or not self._connected:
            _LOGGER.debug("Cannot send query: not connected")
            return
        self.send_command(vin, service_type=19, msg_content={}, msg_type=4)
        _LOGGER.debug("Published state query for VIN %s", vin)

    # ── Listener topic subscription ──────────────────────────

    def _subscribe_listener_topics(self) -> None:
        """Subscribe to configured listener topics after connect."""
        if not self._listener_enabled or not self._listener_topics:
            return
        if not self._client:
            return

        _LOGGER.info(
            "Subscribing to %d listener topics", len(self._listener_topics)
        )
        for topic in self._listener_topics:
            try:
                result, mid = self._client.subscribe(topic, qos=0)
                if mid is not None:
                    self._subscription_mids[mid] = topic
                _LOGGER.info(
                    "Subscribed to listener topic: %s (mid=%s, rc=%s)",
                    topic, mid, result,
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to subscribe to listener topic: %s", topic,
                    exc_info=True,
                )

    # ── paho callbacks (called from network thread) ──────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        """Handle connection. CallbackAPIVersion.VERSION2 returns ReasonCode."""
        rc_str = str(rc)
        if rc_str == "Success" or rc_str == "0" or rc == 0:
            _LOGGER.info("XPENG MQTT connected, subscribing to %s and %s",
                         self.recv_topic, self.recv_topic_v2)
            self._connected = True
            self._last_msg_ts = time.time()
            client.subscribe(self.recv_topic, qos=1)
            # V2 protocol — log payloads via the listener path so we can
            # characterise their shape before deciding how to parse them.
            client.subscribe(self.recv_topic_v2, qos=1)
            # Subscribe to listener topics after primary subscription
            self._subscribe_listener_topics()
        else:
            _LOGGER.error("XPENG MQTT connection refused: rc=%s (rc_str=%s)", rc, rc_str)
            self._connected = False
            # A refused CONNECT does not necessarily raise _on_disconnect, so the
            # sync there may never have run. Do it here too, before anything
            # else: whichever way the callback below resolves, paho's next retry
            # should present the credential we actually hold. Without this the
            # retry repeats the same refused password and the refusal repeats.
            self._apply_current_credentials()
            # Trigger credential refresh on any auth failure
            if self._auth_failure_callback:
                _LOGGER.warning("Triggering credential refresh callback")
                self._auth_failure_callback()
            else:
                _LOGGER.warning("No auth_failure_callback set — auto-refresh not configured")

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any = None,
        rc: Any = None,
        properties: Any = None,
    ) -> None:
        """Handle disconnection. Paho will auto-reconnect."""
        self._connected = False
        self._subscription_mids.clear()
        rc_str = str(rc) if rc is not None else "None"
        if rc_str in ("Normal disconnection", "0"):
            _LOGGER.debug("XPENG MQTT disconnected normally")
        else:
            _LOGGER.warning(
                "XPENG MQTT disconnected: rc=%s (paho will auto-reconnect)",
                rc_str,
            )
        # Before paho retries, make sure it will present the password we hold
        # rather than the one the client was built with.
        self._apply_current_credentials()

    def _apply_current_credentials(self) -> None:
        """Put the current password onto the live client for paho's next attempt.

        `username_pw_set` sets what the CONNECT packet will carry, so calling it
        again on an existing client changes the credential paho presents when it
        retries. That matters because the credential refresh retires the previous
        password every ~45s while paho's retry is up to 60s away
        (`reconnect_delay_set(10, 30)`), so without this a retry presents a
        password the broker has already retired and is refused as "Not
        authorized" - 15 times in three hours on 2026-10-07, none of them a
        genuinely expired login. The first retry is ~10s out, comfortably inside
        the rotation window, so it lands on a valid credential.

        `_client_password` is updated too: it records what the client will
        present, so `credentials_superseded` stays an accurate signal rather than
        claiming a staleness that has just been corrected.
        """
        client = self._client
        if client is None or not self._password:
            return
        try:
            client.username_pw_set(self._client_id, self._password)
            self._client_password = self._password
        except Exception:
            _LOGGER.debug("Could not refresh live client credentials", exc_info=True)

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: Any,
        reason_codes: Any,
        properties: Any = None,
    ) -> None:
        """Handle subscription acknowledgement. Track discovery results."""
        mid_int = int(mid) if mid is not None else -1
        topic = self._subscription_mids.pop(mid_int, None)

        _LOGGER.info(
            "SUBACK mid=%s topic=%s reason_codes=%s type=%s pending=%s",
            mid, topic, reason_codes, type(reason_codes).__name__,
            list(self._subscription_mids.values()),
        )

        if topic is None:
            # This is the primary recv_topic subscription, ignore
            return

        # Check reason codes for acceptance/rejection
        # MQTTv3.1.1 SUBACK returns granted QoS (0,1,2) or 128 for failure
        accepted = True
        reason = "Granted"
        if reason_codes is not None:
            codes = reason_codes if isinstance(reason_codes, list) else [reason_codes]
            for code in codes:
                code_val = int(code) if hasattr(code, '__int__') else -1
                code_str = str(code)
                # MQTTv3.1.1: 0/1/2 = granted QoS, 128 = failure
                # MQTTv5: >= 128 = error
                if code_val >= 128:
                    accepted = False
                    reason = code_str
                    break
                elif code_val in (0, 1, 2):
                    reason = f"Granted QoS {code_val}"
                else:
                    # Check string representation for errors
                    if "error" in code_str.lower() or "not" in code_str.lower():
                        accepted = False
                        reason = code_str
                        break
                    reason = code_str

        if self._subscription_result_callback:
            try:
                self._subscription_result_callback(topic, accepted, reason)
            except Exception:
                _LOGGER.exception(
                    "Error in subscription result callback for %s", topic
                )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Handle incoming message. Route to vehicle state or listener."""
        self._last_msg_ts = time.time()
        topic = msg.topic
        payload_bytes = msg.payload

        # Check if this is a listener message (not our primary recv_topic)
        if topic != self.recv_topic:
            self._handle_listener_message(topic, payload_bytes)
            return

        # Primary vehicle-state topic: this is the only thing that counts as
        # "the car is delivering data" for the silence watchdog.
        self._last_state_msg_ts = time.time()

        # Primary vehicle state message handling
        try:
            payload_len = len(payload_bytes)
            data = json.loads(payload_bytes.decode("utf-8"))
            stype = data.get("service_type")
            mtype = data.get("msg_type")

            # Wake any synchronous waiter (send_command_wait) for this reply
            # before the type-specific handling below runs.
            self._resolve_pending(data, stype)

            content = data.get("msg_content", {})
            code = content.get("code") if isinstance(content, dict) else None

            # Push notifications (msg_type=3) — fire notification callback
            if mtype == 3:
                _LOGGER.info(
                    "Push notification: service_type=%s content=%s",
                    stype,
                    json.dumps(content) if isinstance(content, dict) else str(content)[:500],
                )
                self._fire_notification(data)
                return

            # Command responses (service_type != 19) — log and fire feedback callback
            if stype != 19:
                _LOGGER.info(
                    "Command response: service_type=%s msg_type=%s content=%s",
                    stype, mtype,
                    json.dumps(content) if isinstance(content, dict) else str(content)[:500],
                )
                self._fire_command_feedback(data)
                return

            if mtype != 1:
                _LOGGER.debug(
                    "Ignoring message: service_type=%s msg_type=%s", stype, mtype
                )
                return

            if code != 200:
                _LOGGER.info("Non-200 state response: code=%s msg=%s",
                    code, content.get("msg", "") if isinstance(content, dict) else "")
                return

            instant = content.get("instant", {})
            # A true full state has ext + sufficient other sections.
            # We require ext (vehicle mode/sentinel/etc.) plus at least one
            # of: charge, drive, power, odometer, door — this handles both
            # charging mode (charge populated) and driving mode (drive/power
            # populated but charge may be empty since the car isn't plugged in).
            # During charge-only partials the car sends ONLY a "charge" dict
            # with empty ext — treat those as partial, not full state.
            has_ext = bool(instant.get("ext"))
            has_vehicle_data = any(
                bool(instant.get(k))
                for k in ("charge", "drive", "power", "odometer", "door")
            )
            is_full = has_ext and has_vehicle_data

            if is_full:
                soc = instant.get("charge", {}).get("battery_soc") if instant.get("charge") else None
                ext = instant.get("ext", {})
                protect = instant.get("protectMode", {})
                _LOGGER.info(
                    "Full state: SOC=%s%% (%dB) ext=%s protect=%s keys=%s",
                    soc, payload_len,
                    json.dumps(ext),
                    json.dumps(protect),
                    list(instant.keys()),
                )
                # Store as last full content for merging with partial responses
                self._last_full_content = content
                # Pass the full parsed data to the coordinator callback
                vehicle_data: dict[str, Any] = {
                    "vin": data.get("target_id", ""),
                    "msg_content": content,
                    "has_fresh_energy_data": bool(
                        instant.get("charge") or instant.get("odometer")
                    ),
                }
                if self._state_callback is not None:
                    # This is the point where a message stops being a ping and
                    # becomes data: stamp it, so staleness can be measured
                    # without stubs resetting the clock.
                    self._last_meaningful_state_ts = time.time()
                    self._state_callback(vehicle_data)
            elif self._last_full_content is not None:
                # Partial response — merge non-empty instant sections into last
                # full state so sensors update with whatever data the car sent.
                changed_keys = [k for k, v in instant.items() if v]
                has_interval = bool(content.get("interval"))
                if not changed_keys and not has_interval and not content.get(
                    "digital_vehicle"
                ):
                    # Pure stub: an asleep car's ~248-byte ack with nothing new
                    # to merge. Do NOT propagate it as a fresh push — that
                    # freezes every sensor at its last value AND (because it
                    # bumps last_data_timestamp) hides the MQTT silence from
                    # the REST widget-merge fallback, so sensors can sit stale
                    # for hours during a stuck cloud fanout. Let the silence
                    # show.
                    _LOGGER.debug(
                        "Stub response (%dB) — ignoring, no new data", payload_len
                    )
                    return
                merged = dict(self._last_full_content)
                merged_instant = dict(merged.get("instant", {}))
                for key, val in instant.items():
                    if val:
                        merged_instant[key] = val
                merged["instant"] = merged_instant
                if "interval" in content:
                    merged["interval"] = content["interval"]
                if "digital_vehicle" in content:
                    merged["digital_vehicle"] = content["digital_vehicle"]
                _LOGGER.debug(
                    "Partial state (%dB, keys=%s) — merged with last full state",
                    payload_len,
                    list(instant.keys()),
                )
                vehicle_data = {
                    "vin": data.get("target_id", ""),
                    "msg_content": merged,
                    # Only true if THIS message's own instant carried fresh
                    # charge/odometer data — a partial that only refreshes
                    # e.g. door/window/ext must not be mistaken for a fresh
                    # battery/range reading (see mqtt_stale gate in
                    # coordinator._merge_widget_state_if_mqtt_stale).
                    "has_fresh_energy_data": bool(
                        instant.get("charge") or instant.get("odometer")
                    ),
                }
                if self._state_callback is not None:
                    # This is the point where a message stops being a ping and
                    # becomes data: stamp it, so staleness can be measured
                    # without stubs resetting the clock.
                    self._last_meaningful_state_ts = time.time()
                    self._state_callback(vehicle_data)
            else:
                _LOGGER.debug(
                    "Minimal response (no prior data), %d bytes — ignoring",
                    payload_len,
                )

        except Exception:
            _LOGGER.exception("Error parsing XPENG MQTT message")

    def _handle_listener_message(self, topic: str, payload: bytes) -> None:
        """Handle a message received on a listener topic."""
        try:
            # Try to decode as UTF-8 text/JSON
            payload_str = payload.decode("utf-8", errors="replace")
        except Exception:
            payload_str = f"<binary {len(payload)} bytes>"

        # One line per message is fine for a handful of topics. Against "#" - which the
        # listener accepts, and which includes the car's 1 Hz CAN broadcast - it is a log
        # flood on its own, before any of the entity work. First message per topic is INFO
        # so a new topic is still discoverable; the rest drop to debug.
        if topic in self._listener_logged_topics:
            _LOGGER.debug(
                "Listener message on %s (%d bytes): %s",
                topic, len(payload), payload_str[:500],
            )
        else:
            self._listener_logged_topics.add(topic)
            _LOGGER.info(
                "Listener message on %s (%d bytes): %s",
                topic, len(payload), payload_str[:500],
            )

        # Check for push notification (msg_type=3)
        try:
            data = json.loads(payload_str)
            if isinstance(data, dict) and data.get("msg_type") == 3:
                self._fire_notification(data)
        except (json.JSONDecodeError, ValueError):
            pass

        if self._listener_callback:
            self._listener_callback(topic, payload_str)

    def _fire_notification(self, data: dict[str, Any]) -> None:
        """Extract push notification fields and invoke callback."""
        content = data.get("msg_content", {})
        if not isinstance(content, dict):
            content = {}
        notification = {
            "service_type": data.get("service_type"),
            "msg_id": data.get("msg_id", ""),
            "code": content.get("code"),
            "message": content.get("msg", ""),
        }
        # Include any extra fields from msg_content
        for key, val in content.items():
            if key not in ("code", "msg"):
                notification[key] = val
        _LOGGER.info("Push notification parsed: %s", notification)
        if self._notification_callback:
            self._notification_callback(notification)

    def _fire_command_feedback(self, data: dict[str, Any]) -> None:
        """Extract command response fields and invoke callback."""
        content = data.get("msg_content", {})
        if not isinstance(content, dict):
            content = {}
        feedback = {
            "service_type": data.get("service_type"),
            "msg_type": data.get("msg_type"),
            "msg_id": data.get("msg_id", ""),
            "code": content.get("code"),
            "message": content.get("msg", ""),
            "content": content,
        }
        if self._command_feedback_callback:
            self._command_feedback_callback(feedback)
