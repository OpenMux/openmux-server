"""Tests for the shared PDU driver read-backoff helper.

Covers the fixed backoff envelope (base 30 s, one failed probe per
lapsed window, doubling, 300 s cap), reset on success, and the
injectable clock. Pure unit tests; no fakes needed because the clock is
a callable the tests advance by hand.
"""

import pytest

from openmux.server.adapters.power_drivers.readbackoff import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    ReadBackoff,
)


class _Clock:
    """Monotonic-style clock controlled from the test."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make():
    clock = _Clock()
    return ReadBackoff("test-pdu", now=clock), clock


def test_initially_not_in_backoff():
    rb, clock = _make()
    assert rb.in_backoff() is False
    clock.advance(100)
    assert rb.in_backoff() is False


def test_first_failure_starts_base_window():
    rb, clock = _make()
    window = rb.note_failure()
    assert window == pytest.approx(BACKOFF_BASE_SECONDS)
    assert rb.in_backoff() is True
    clock.advance(BACKOFF_BASE_SECONDS - 1)
    assert rb.in_backoff() is True
    clock.advance(1)
    assert rb.in_backoff() is False


def test_failed_probes_after_lapse_double_the_window():
    rb, clock = _make()
    rb.note_failure()  # window 30 (until t=30)
    clock.advance(31)
    assert rb.in_backoff() is False
    rb.note_failure()  # streak 2: window 60 (until t=31+60)
    assert rb._window_until == pytest.approx(31 + 60)
    clock.advance(61)
    rb.note_failure()  # streak 3: window 120
    assert rb._window_until == pytest.approx(31 + 61 + 120)
    clock.advance(121)
    rb.note_failure()  # streak 4: window 240
    assert rb._window_until == pytest.approx(31 + 61 + 121 + 240)


def test_window_caps_at_cap_seconds():
    rb, clock = _make()
    probe_t = 0.0
    lengths = []
    for _ in range(6):
        clock.t = probe_t
        lengths.append(rb.note_failure())
        probe_t = clock.t + lengths[-1] + 1  # one failed probe after each lapse
    assert lengths[:4] == [30.0, 60.0, 120.0, 240.0]
    assert all(l == BACKOFF_CAP_SECONDS for l in lengths[4:])


def test_success_resets_failure_streak():
    rb, clock = _make()
    rb.note_failure()
    clock.advance(5)
    rb.note_success()
    assert rb.in_backoff() is False
    clock.advance(100)
    assert rb.in_backoff() is False
    # The next failure restarts the envelope from the base window.
    window = rb.note_failure()
    assert window == pytest.approx(BACKOFF_BASE_SECONDS)


def test_success_outside_backoff_is_a_noop():
    rb, clock = _make()
    rb.note_success()
    assert rb.in_backoff() is False
    clock.advance(10)
    rb.note_success()
    assert rb.in_backoff() is False


def test_constants():
    assert BACKOFF_BASE_SECONDS == 30.0
    assert BACKOFF_CAP_SECONDS == 300.0
