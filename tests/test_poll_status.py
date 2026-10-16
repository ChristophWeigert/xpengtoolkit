#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the polling-status vocabulary and arithmetic. Standard library only.

    python3 tests/test_poll_status.py
    python3 -m unittest discover -s tests -v

Loaded by path rather than imported as `custom_components.xpeng.poll_status`,
for the same reason as test_watchdog_policy: that would execute the package's
__init__, which imports Home Assistant, which is not installed here and must not
need to be.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

_MODULE_PATH = _component.module_path("poll_status.py")
_spec = importlib.util.spec_from_file_location("poll_status", _MODULE_PATH)
assert _spec and _spec.loader
poll = importlib.util.module_from_spec(_spec)
sys.modules["poll_status"] = poll
_spec.loader.exec_module(poll)

SCAN = 60.0
PARKED_SCAN = 3600.0
THRESHOLD = 300.0


class TestCadence(unittest.TestCase):
    """The cadence must match what `_send_periodic_query` actually does.

    It uses `coordinator.is_charging or coordinator.vehicle_parked is False`.
    A sensor that disagreed with the timer would be reporting the one thing it
    exists to report, wrongly.
    """

    def test_charging_is_active(self):
        self.assertTrue(poll.is_active(True, True))

    def test_driving_is_active(self):
        """`vehicle_parked is False` means moving, whatever the charge state."""
        self.assertTrue(poll.is_active(False, False))

    def test_parked_and_not_charging_is_not_active(self):
        self.assertFalse(poll.is_active(True, False))

    def test_unknown_parked_state_is_not_active(self):
        """A tri-state: None is "no data yet", and no data is not a moving car."""
        self.assertFalse(poll.is_active(None, False))

    def test_charging_wins_over_a_parked_reading(self):
        """Plugged in and charging while the last shift state said P."""
        self.assertTrue(poll.is_active(True, True))

    def test_active_uses_the_short_interval(self):
        self.assertEqual(
            poll.query_interval_seconds(False, False, SCAN, PARKED_SCAN), SCAN
        )

    def test_parked_uses_the_long_interval(self):
        self.assertEqual(
            poll.query_interval_seconds(True, False, SCAN, PARKED_SCAN), PARKED_SCAN
        )


class TestNextQuery(unittest.TestCase):
    def test_due_at_the_last_send_plus_the_interval(self):
        self.assertEqual(poll.next_query_at(1000.0, 1000.0, SCAN), 1060.0)

    def test_a_query_already_overdue_reads_as_now(self):
        """Never a timestamp in the past.

        A `device_class: timestamp` sensor renders a past value as "5 minutes
        ago", which for a *scheduled* action reads as something that should
        already have happened and did not.
        """
        self.assertEqual(poll.next_query_at(1000.0, 5000.0, SCAN), 5000.0)

    def test_exactly_due_is_now_not_a_moment_ago(self):
        self.assertEqual(poll.next_query_at(1000.0, 1060.0, SCAN), 1060.0)

    def test_one_second_early_still_reports_the_due_time(self):
        self.assertEqual(poll.next_query_at(1000.0, 1059.0, SCAN), 1060.0)

    def test_the_parked_interval_is_honoured(self):
        self.assertEqual(
            poll.next_query_at(1000.0, 1000.0, PARKED_SCAN), 1000.0 + PARKED_SCAN
        )


class TestDescribe(unittest.TestCase):
    def _describe(self, **overrides):
        kwargs = dict(
            connected=True,
            active=False,
            silence_seconds=0.0,
            silence_threshold=THRESHOLD,
            escalated=False,
        )
        kwargs.update(overrides)
        return poll.describe(**kwargs)

    def test_parked_and_quiet_is_parked(self):
        self.assertEqual(self._describe(), poll.PARKED)

    def test_parked_and_charging_is_active(self):
        self.assertEqual(self._describe(active=True), poll.ACTIVE)

    def test_a_down_socket_outranks_everything(self):
        """The most actionable fact wins, whatever else is true."""
        self.assertEqual(
            self._describe(
                connected=False, active=True, silence_seconds=9999.0, escalated=True
            ),
            poll.RECONNECTING,
        )

    def test_silence_past_the_threshold_is_silent(self):
        self.assertEqual(self._describe(silence_seconds=THRESHOLD), poll.SILENT)

    def test_one_second_short_of_the_threshold_is_not_silent(self):
        self.assertEqual(self._describe(silence_seconds=THRESHOLD - 1), poll.PARKED)

    def test_an_escalation_is_only_reported_while_still_silent(self):
        """Once data is flowing again the escalation is history, not status."""
        self.assertEqual(
            self._describe(silence_seconds=THRESHOLD, escalated=True), poll.ESCALATED
        )
        self.assertEqual(
            self._describe(silence_seconds=0.0, escalated=True), poll.PARKED
        )

    def test_silence_while_active_is_still_silent(self):
        """A stalled connection during a drive is the case worth shouting about."""
        self.assertEqual(
            self._describe(active=True, silence_seconds=THRESHOLD), poll.SILENT
        )

    def test_every_status_is_one_of_the_named_ones(self):
        seen = {
            self._describe(),
            self._describe(active=True),
            self._describe(connected=False),
            self._describe(silence_seconds=THRESHOLD),
            self._describe(silence_seconds=THRESHOLD, escalated=True),
        }
        self.assertEqual(
            seen,
            {poll.PARKED, poll.ACTIVE, poll.RECONNECTING, poll.SILENT, poll.ESCALATED},
        )




class TestEscalationFlagIsLive(unittest.TestCase):
    """`escalated` must clear once data returns.

    The watchdog used to publish `bool(last_escalation)`, which is sticky: one
    force-upload set it true and it stayed true for the rest of the session, so
    the Polling status sensor read "Escalated" long after the stall had cleared
    and normal data was arriving. A historical flag cannot answer "what is it
    doing right now", which is the whole job of that sensor.

    These pin the derivation the watchdog now uses: true only while the stall is
    still past the threshold AND the force-upload for it has been spent.
    """

    AFTER = 30 * 60
    BACKOFF = 12 * 3600

    def _flag(self, stale, last_escalation, now):
        return poll.is_escalated(
            stale, last_escalation, now,
            escalate_after=self.AFTER, escalate_backoff=self.BACKOFF,
        )

    def test_not_escalated_before_the_threshold(self):
        """Nothing has been tried yet, so nothing has been escalated."""
        self.assertFalse(self._flag(stale=60.0, last_escalation=0.0, now=1000.0))

    def test_escalated_while_stale_past_the_threshold_with_a_spent_upload(self):
        now = 10 * 3600.0
        self.assertTrue(
            self._flag(stale=self.AFTER + 60, last_escalation=now - 3600, now=now)
        )

    def test_clears_once_data_returns(self):
        """THE regression: recovery must clear it, not leave it stuck."""
        now = 10 * 3600.0
        spent = now - 3600
        self.assertTrue(self._flag(stale=self.AFTER + 60, last_escalation=spent, now=now))
        # data arrived, so staleness cleared
        self.assertFalse(self._flag(stale=0.0, last_escalation=spent, now=now))

    def test_still_pending_is_not_reported_as_escalated(self):
        """Pending and spent are different states; the report must not conflate."""
        now = 10 * 3600.0
        stale = self.AFTER + 60
        self.assertTrue(
            poll.escalation_pending(stale, 0.0, now,
                                    escalate_after=self.AFTER,
                                    escalate_backoff=self.BACKOFF)
        )
        self.assertFalse(
            self._flag(stale=stale, last_escalation=0.0, now=now),
            "an upload that has not been sent is not an escalation yet",
        )

    def test_inside_the_backoff_window_still_reports_escalated(self):
        """The stall persists and we are holding off: that IS escalated."""
        now = 10 * 3600.0
        self.assertTrue(
            self._flag(stale=self.AFTER + 60, last_escalation=now - 60, now=now)
        )

    def test_past_the_backoff_it_is_pending_again_not_escalated(self):
        """Once the backoff expires a new upload is due, so it is not 'spent'."""
        now = 10 * 3600.0
        self.assertFalse(
            self._flag(stale=self.AFTER + 60,
                       last_escalation=now - self.BACKOFF - 1, now=now)
        )


class TestFormatAge(unittest.TestCase):
    """The credential sensor's state must be able to show its own cycle.

    The refresh loop mints a password every 45s, so the state has to change
    second-by-second below a minute or the entity looks dead while working.
    """

    def test_seconds_below_a_minute(self):
        self.assertEqual(poll.format_age(0), "0s")
        self.assertEqual(poll.format_age(41), "41s")
        self.assertEqual(poll.format_age(59), "59s")

    def test_exactly_a_minute_switches_to_minutes(self):
        self.assertEqual(poll.format_age(60), "1m")

    def test_minutes(self):
        self.assertEqual(poll.format_age(90), "1m")
        self.assertEqual(poll.format_age(59 * 60 + 59), "59m")

    def test_hours_and_minutes(self):
        self.assertEqual(poll.format_age(3600), "1h 0m")
        self.assertEqual(poll.format_age(3660), "1h 1m")
        self.assertEqual(poll.format_age(3600 * 5 + 120), "5h 2m")

    def test_fractions_truncate_rather_than_round(self):
        """An age must never read as older than it is."""
        self.assertEqual(poll.format_age(41.9), "41s")
        self.assertEqual(poll.format_age(59.99), "59s")

    def test_a_negative_age_reads_as_zero(self):
        """Clock adjustment must not produce "-3s" on a dashboard."""
        self.assertEqual(poll.format_age(-5), "0s")

    def test_a_healthy_cycle_is_visible_as_changing_values(self):
        """The actual point: 0..45s round-trip must not render as one value."""
        seen = {poll.format_age(s) for s in range(0, 45)}
        self.assertGreater(len(seen), 40)
        self.assertNotIn("0m", seen)


class TestTheChargingHint(unittest.TestCase):
    """An external charger can raise the cadence without claiming the car is awake.

    Measured 2026-10-17: charging for ~15 minutes, the car sent nothing until a
    passive query was answered; the charger knew about the 7.4 kW the whole time. So
    the hint has to make us *ask* sooner - and nothing else, because the periodic
    query is the only thing here that can wake a car.
    """

    def test_a_hint_raises_the_cadence(self):
        self.assertEqual(
            poll.query_interval_seconds(
                parked=True, is_charging=False, scan_interval=60,
                parked_scan_interval=900, charging_hint=True,
            ),
            60,
        )

    def test_without_a_hint_a_parked_car_keeps_the_slow_cadence(self):
        self.assertEqual(
            poll.query_interval_seconds(
                parked=True, is_charging=False, scan_interval=60,
                parked_scan_interval=900,
            ),
            900,
        )

    def test_charging_still_wins(self):
        self.assertEqual(
            poll.query_interval_seconds(
                parked=False, is_charging=True, scan_interval=60,
                parked_scan_interval=900, charging_hint=True,
            ),
            60,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNotificationDelay(unittest.TestCase):
    """How late a Message Centre message was, given only its own timestamp.

    The API's `createTime` has no timezone, so a naive value is read as local time -
    which is what XPENG shows the account holder. Everything else is rejected rather
    than guessed, because this number is what decides whether the poll interval is
    worth lowering.
    """

    NOW = 1_700_000_000.0

    def test_a_naive_stamp_is_read_as_local_time(self):
        stamp = datetime.fromtimestamp(self.NOW - 300).isoformat()
        self.assertAlmostEqual(poll.notification_delay_seconds(stamp, self.NOW), 300.0, delta=1)

    def test_the_raw_four_digit_fraction_parses(self):
        """createTime arrives as "YYYY-MM-DD HH:MM:SS.0000", which fromisoformat
        refused before 3.11 - the API layer normalises it, but the raw value can
        still reach an attribute when that normalisation fails."""
        stamp = datetime.fromtimestamp(self.NOW - 120).strftime("%Y-%m-%d %H:%M:%S.0000")
        delay = poll.notification_delay_seconds(stamp, self.NOW)
        self.assertIsNotNone(delay)
        self.assertAlmostEqual(delay, 120.0, delta=1)

    def test_an_explicit_offset_is_respected(self):
        stamp = datetime.fromtimestamp(self.NOW - 60, tz=timezone.utc).isoformat()
        self.assertAlmostEqual(poll.notification_delay_seconds(stamp, self.NOW), 60.0, delta=1)

    def test_nothing_usable_is_none_rather_than_a_guess(self):
        for bad in ("", None, "yesterday", "2026-13-45 99:99:99"):
            self.assertIsNone(poll.notification_delay_seconds(bad, self.NOW), repr(bad))

    def test_a_future_stamp_stays_negative(self):
        """Clock skew is information; clamping it would hide a real problem."""
        stamp = datetime.fromtimestamp(self.NOW + 500).isoformat()
        self.assertLess(poll.notification_delay_seconds(stamp, self.NOW), 0)
