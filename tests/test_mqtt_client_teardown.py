#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The MQTT client must actually let go of a connection when told to.

Two clients under one XPENG account fight over the broker: it authorises one and
refuses the other as "Not authorized", every refusal triggers a credential
refresh and a rebuild, and each rebuild resets the entities - which is how an
install ends up flapping between ~20 and 124 unknown entities. Measured on a live
install on 2026-10-11, after a device-id change left a stale client behind:

    two clientIds for one account, two live paho clients, 97/124 unknown

paho names its network thread ``paho-mqtt-client-<clientId>`` at construction, and
``connect()`` always tears down its own client first - so two such threads mean two
*client objects*, i.e. a setup that orphaned its predecessor rather than a
double-connect inside one. ``async_setup_entry`` now stops a predecessor for the
same entry, and these tests pin the teardown contract that has to hold for that to
be enough.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

try:
    _MODULE_PATH = _component.module_path("mqtt_client.py")
    _spec = importlib.util.spec_from_file_location("mqtt_client_t", _MODULE_PATH)
    assert _spec and _spec.loader
    mqtt_client = importlib.util.module_from_spec(_spec)
    sys.modules["mqtt_client_t"] = mqtt_client
    _spec.loader.exec_module(mqtt_client)
except ImportError as exc:  # paho is the only third-party import
    raise unittest.SkipTest(f"mqtt_client.py could not be imported: {exc}")


class _FakePaho:
    """Stand-in for paho's Client that records what teardown did to it."""

    def __init__(self, fail_on=()):
        self.calls: list[str] = []
        self.detached: dict[str, object] = {}
        self._fail_on = set(fail_on)
        self.on_connect = "original"
        self.on_message = "original"
        self.on_disconnect = "original"
        self.on_subscribe = "original"

    def _record(self, name):
        self.calls.append(name)
        if name in self._fail_on:
            raise RuntimeError(f"{name} blew up")

    def disconnect(self):
        self._record("disconnect")

    def loop_stop(self):
        self._record("loop_stop")

    def __setattr__(self, key, value):
        if key in ("on_connect", "on_message", "on_disconnect", "on_subscribe"):
            object.__setattr__(self, "detached", {**getattr(self, "detached", {}), key: value})
        object.__setattr__(self, key, value)


def _client(fake=None):
    client = mqtt_client.XPengMQTTClient(
        client_id="xmart:app:10225403:4dc51b29c3dee8b8ea6f7648ce407ffa",
        password="pw",
        server="broker.example:8885",
        user_id="10225403",
    )
    if fake is not None:
        client._client = fake  # no public setter; the seam the tests need
    return client


class TestTeardownOrder(unittest.TestCase):
    def test_disconnect_is_sent_before_the_loop_is_stopped(self):
        """paho writes the DISCONNECT packet from the network thread.

        Stopping the loop first can leave that packet unwritten, so the broker
        keeps the session - and a session the broker still attributes to this
        clientId is what makes the next connect look like a second subscriber.
        """
        fake = _FakePaho()
        _client(fake).disconnect()
        self.assertEqual(fake.calls, ["disconnect", "loop_stop"])

    def test_callbacks_are_detached(self):
        """A thread that outlives this call must not keep feeding the integration."""
        fake = _FakePaho()
        _client(fake).disconnect()
        for attr in ("on_connect", "on_message", "on_disconnect", "on_subscribe"):
            self.assertIsNone(getattr(fake, attr), attr)

    def test_the_client_is_released(self):
        fake = _FakePaho()
        client = _client(fake)
        client.disconnect()
        self.assertIsNone(client._client)
        self.assertFalse(client.connected)


class TestTeardownSafety(unittest.TestCase):
    def test_second_disconnect_is_a_no_op(self):
        """Unload can run after a replace already tore the client down."""
        fake = _FakePaho()
        client = _client(fake)
        client.disconnect()
        client.disconnect()  # must not raise, must not touch the client again
        self.assertEqual(fake.calls, ["disconnect", "loop_stop"])

    def test_no_client_is_not_an_error(self):
        _client().disconnect()  # never connected

    def test_a_client_that_raises_does_not_break_teardown(self):
        """paho raises on a broken socket; teardown still has to finish."""
        fake = _FakePaho(fail_on=("disconnect",))
        client = _client(fake)
        client.disconnect()
        self.assertIn("loop_stop", fake.calls)
        self.assertIsNone(client._client)

    def test_a_loop_stop_that_raises_is_swallowed(self):
        fake = _FakePaho(fail_on=("loop_stop",))
        client = _client(fake)
        client.disconnect()
        self.assertIsNone(client._client)


if __name__ == "__main__":
    unittest.main()
