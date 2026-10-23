"""Correlate commands across the real publish/wait/receive path without a car."""
from _import_isolation import ImportIsolation
_IMPORTS = ImportIsolation()

import json
import threading
from types import SimpleNamespace
import unittest

from test_mqtt_state_delivery import _client, _Msg


class AckMatching(unittest.TestCase):
    def make(self, **request):
        c, _ = _client()
        e = {"event": threading.Event(), "result": None, "service_type": 4,
             "request": request or {"cmd_type": 1, "cmd_value": 1}}
        c._pending = {"original-id": e}
        return c, e

    def test_notifications_and_command_echoes_do_not_resolve_waiter(self):
        c, e = self.make()
        for msg_type in (2, 3, 4, None):
            c._resolve_pending({"msg_id": "original-id", "msg_type": msg_type}, 4)
        self.assertFalse(e["event"].is_set())

    def test_first_correlated_reply_wins(self):
        c, e = self.make()
        refusal = {"msg_id": "original-id", "msg_type": 1, "msg_content": {"code": 10401}}
        c._resolve_pending(refusal, 4)
        c._resolve_pending({**refusal, "msg_content": {"code": 200}}, 4)
        self.assertTrue(e["event"].is_set())
        self.assertIs(e["result"], refusal)

    def test_other_subsystem_never_satisfies_even_an_exact_id(self):
        c, e = self.make()
        c._resolve_pending({"msg_id": "original-id", "msg_type": 1}, 12)
        self.assertFalse(e["event"].is_set())

    def test_generic_same_subsystem_reply_cannot_confirm_a_command(self):
        c, e = self.make()
        for code in (200, 10401):
            for msg_id in (None, "foreign-id"):
                c._resolve_pending({"msg_id": msg_id, "msg_type": 1,
                                    "msg_content": {"code": code}}, 4)
        self.assertFalse(e["event"].is_set())

    def test_rewritten_id_requires_both_echoed_request_fields(self):
        c, e = self.make()
        for echo in ({"cmd_type": 1}, {"cmd_value": 1},
                     {"cmd_type": 2, "cmd_value": 1},
                     {"cmd_type": 1, "cmd_value": 0}):
            c._resolve_pending({"msg_id": "rewritten", "msg_type": 1,
                                "msg_content": {"code": 200, **echo}}, 4)
        self.assertFalse(e["event"].is_set())
        c._resolve_pending({"msg_id": "rewritten", "msg_type": 1,
                            "msg_content": {"code": 200, "cmd_type": 1, "cmd_value": 1}}, 4)
        self.assertTrue(e["event"].is_set())

    def test_ambiguous_identical_waiters_do_not_accept_fallback(self):
        c, e = self.make()
        other = {**e, "event": threading.Event()}
        c._pending["second-id"] = other
        c._resolve_pending({"msg_type": 1, "msg_content": {
            "code": 200, "cmd_type": 1, "cmd_value": 1}}, 4)
        self.assertFalse(e["event"].is_set())
        self.assertFalse(other["event"].is_set())

    def test_exact_id_with_explicit_conflicting_request_is_rejected(self):
        c, e = self.make()
        c._resolve_pending({"msg_id": "original-id", "msg_type": 1,
                            "msg_content": {"code": 200, "cmd_type": 2, "cmd_value": 90}}, 4)
        self.assertFalse(e["event"].is_set())

    def test_publish_wait_receive_ignores_foreign_success_and_refusal(self):
        for subsystem in (3, 4, 12):
            with self.subTest(subsystem=subsystem):
                c, _ = _client()
                published, finished = threading.Event(), threading.Event()
                packets, result = [], []

                def publish(topic, payload, qos):
                    packets.append(json.loads(payload))
                    published.set()
                    return SimpleNamespace(rc=0)

                c._connected = True
                c._client = SimpleNamespace(publish=publish)

                def wait():
                    result.append(c.send_command_wait(
                        "SYNTHETIC_VIN", subsystem, {"cmd_type": 1, "cmd_value": 1}, timeout=1.5
                    ))
                    finished.set()

                worker = threading.Thread(target=wait)
                worker.start()
                try:
                    self.assertTrue(published.wait(1))
                    for code in (200, 10401):
                        c._on_message(None, None, _Msg({
                            "service_type": subsystem, "msg_type": 1,
                            "msg_id": "another-command", "msg_content": {"code": code},
                        }))
                        self.assertFalse(finished.wait(0.02))
                    own = {"service_type": subsystem, "msg_type": 1,
                           "msg_id": packets[0]["msg_id"], "msg_content": {"code": 200}}
                    c._on_message(None, None, _Msg(own))
                    self.assertTrue(finished.wait(1))
                    self.assertEqual(result, [own])
                finally:
                    worker.join(2)
                self.assertFalse(worker.is_alive())


_IMPORTS.finish(globals())

if __name__ == "__main__":
    unittest.main()
