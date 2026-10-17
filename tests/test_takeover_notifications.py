#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A renewal login is not a car notification.

The broker uses one envelope (`msg_type=3`) for both real notifications and the
`ACCOUNT_KICK_OFF` control message (`service_type` 17 / code 4003007). The
renewal app logs in on this account once per renewal — that login *is* the
renewal — so the control message arrives every ~100 minutes. Recording it in the
notification list meant `sensor.xpeng_<vehicle>_last_notification` showed
"Account logged in on another device, local account logged out" almost always
(measured 2026-10-18 on `.162`: count 12, one per renewal) and hid the messages a
user actually wants.

The contract this pins:

1. a takeover notice does NOT reach ``coordinator.record_notification`` (so it
   cannot become the Last-notification state, count or `xpeng_notification` event);
2. it is still recorded where a session event belongs — `last_takeover_at` and
   `account_takeovers` on the polling status, and its own bus event;
3. the polling-status sensor exposes those two attributes.

Loaded as source text: `__init__.py` imports Home Assistant, which this host does
not have, and the repo's structural tests check wiring the same way.
"""

from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT = (ROOT / "custom_components" / "xpeng" / "__init__.py").read_text(encoding="utf-8")
SENSOR = (ROOT / "custom_components" / "xpeng" / "sensor.py").read_text(encoding="utf-8")


def _push_handler() -> str:
    start = INIT.index("def _on_push_notification")
    return INIT[start:INIT.index("mqtt_client.set_notification_callback", start)]


class TakeoverIsNotANotificationTests(unittest.TestCase):
    def test_takeover_branch_returns_before_record_notification(self):
        block = _push_handler()
        takeover = block.index("if is_account_takeover(notification):")
        ret = block.index("return", takeover)
        record = block.index("coordinator.record_notification", takeover)
        self.assertLess(ret, record, "the takeover branch must return, not fall through")

    def test_takeover_still_reaches_the_separate_recorder(self):
        block = _push_handler()
        self.assertIn("_record_account_takeover", block)
        self.assertIn("mqtt_client.note_account_takeover", block)

    def test_real_notifications_still_reach_record_notification(self):
        block = _push_handler()
        # ...on the path after the takeover branch, with no condition on it.
        after = block[block.index("return", block.index("if is_account_takeover")):]
        self.assertIn("coordinator.record_notification", after)

    def test_the_recorder_tracks_the_takeover_on_the_polling_status(self):
        start = INIT.index("def _record_account_takeover")
        block = INIT[start:INIT.index("set_notification_callback", start)]
        self.assertIn("last_takeover_at", block)
        self.assertIn("account_takeovers", block)
        self.assertIn("xpeng_account_takeover", block)

    def test_no_takeover_text_reaches_the_notification_list(self):
        """The recorder must not call record_notification itself."""
        start = INIT.index("def _record_account_takeover")
        block = INIT[start:INIT.index("set_notification_callback", start)]
        self.assertNotIn("record_notification", block)


class PollingStatusExposesTheTakeoverTests(unittest.TestCase):
    def _attrs(self) -> str:
        start = SENSOR.index("class XPengPollingStatusSensor")
        block = SENSOR[start:SENSOR.index("class XPengNextPollSensor", start)]
        return block[block.index("def extra_state_attributes"):]

    def test_last_takeover_is_an_attribute(self):
        self.assertIn('attrs["last_takeover_at"]', self._attrs())

    def test_the_count_is_an_attribute(self):
        self.assertIn('attrs["account_takeovers"]', self._attrs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
