import pytest

from state import (
    PAGE_CONTAINERS,
    PAGE_COUNT,
    PAGE_OVERVIEW,
    ButtonEvent,
    Controller,
    Mode,
)

SHORT, LONG = ButtonEvent.SHORT, ButtonEvent.LONG
A = frozenset({"a"})
AB = frozenset({"a", "b"})
NONE = frozenset()


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ctl(clock):
    return Controller(clock=clock)


def test_starts_dark(ctl):
    assert (ctl.mode, ctl.page) == (Mode.OFF, PAGE_OVERVIEW)


def test_first_press_wakes_to_overview_without_advancing(ctl):
    ctl.on_button(SHORT)
    assert (ctl.mode, ctl.page) == (Mode.AWAKE, PAGE_OVERVIEW)


def test_long_press_while_dark_also_wakes_to_overview(ctl):
    ctl.on_button(LONG)
    assert (ctl.mode, ctl.page) == (Mode.AWAKE, PAGE_OVERVIEW)


def test_short_press_advances_and_wraps(ctl):
    ctl.on_button(SHORT)
    seen = []
    for _ in range(PAGE_COUNT):
        ctl.on_button(SHORT)
        seen.append(ctl.page)
    assert seen == [1, 2, 3, 0]


def test_long_press_returns_to_overview(ctl):
    ctl.on_button(SHORT)
    ctl.on_button(SHORT)
    ctl.on_button(SHORT)
    ctl.on_button(LONG)
    assert (ctl.mode, ctl.page) == (Mode.AWAKE, PAGE_OVERVIEW)


def test_timeout_turns_off_after_30s_but_not_before(ctl, clock):
    ctl.on_button(SHORT)
    clock.advance(29.9)
    ctl.tick()
    assert ctl.mode == Mode.AWAKE
    clock.advance(0.1)
    ctl.tick()
    assert ctl.mode == Mode.OFF


def test_press_restarts_timeout(ctl, clock):
    ctl.on_button(SHORT)
    clock.advance(20)
    ctl.on_button(SHORT)
    clock.advance(20)
    ctl.tick()
    assert ctl.mode == Mode.AWAKE
    clock.advance(10)
    ctl.tick()
    assert ctl.mode == Mode.OFF


def test_unhealthy_wakes_from_dark_on_containers_page(ctl):
    ctl.on_containers(A)
    assert (ctl.mode, ctl.page) == (Mode.ALERT, PAGE_CONTAINERS)


def test_alert_persists_past_30s(ctl, clock):
    ctl.on_containers(A)
    clock.advance(500)
    ctl.tick()
    assert ctl.mode == Mode.ALERT


def test_press_acknowledges_alert_then_times_out_normally(ctl, clock):
    ctl.on_containers(A)
    ctl.on_button(SHORT)
    assert (ctl.mode, ctl.page) == (Mode.AWAKE, PAGE_CONTAINERS)
    clock.advance(29.9)
    ctl.tick()
    assert ctl.mode == Mode.AWAKE
    clock.advance(0.1)
    ctl.tick()
    assert ctl.mode == Mode.OFF
    ctl.on_containers(A)  # still unhealthy, already acknowledged
    assert ctl.mode == Mode.OFF


def test_new_unhealthy_name_alerts_while_awake(ctl):
    ctl.on_containers(A)
    ctl.on_button(SHORT)
    ctl.on_containers(AB)
    assert ctl.mode == Mode.ALERT


def test_flapping_container_realerts_immediately(ctl):
    ctl.on_containers(A)
    ctl.on_button(SHORT)
    ctl.on_containers(NONE)
    assert ctl.mode == Mode.AWAKE
    ctl.on_containers(A)
    assert ctl.mode == Mode.ALERT


def test_alert_clears_to_off_when_all_recover(ctl):
    ctl.on_containers(A)
    ctl.on_containers(NONE)
    assert ctl.mode == Mode.OFF
    ctl.on_button(SHORT)
    assert (ctl.mode, ctl.page) == (Mode.AWAKE, PAGE_OVERVIEW)


def test_unknown_snapshot_keeps_alert_lit(ctl):
    ctl.on_containers(A)
    ctl.on_containers(None)
    assert ctl.mode == Mode.ALERT


def test_unknown_snapshot_does_not_wake(ctl):
    ctl.on_containers(None)
    assert ctl.mode == Mode.OFF


def test_acknowledged_problem_rewakes_after_one_hour(ctl, clock):
    ctl.on_containers(A)
    ctl.on_button(SHORT)
    clock.advance(3599)
    ctl.tick()
    assert ctl.mode != Mode.ALERT
    clock.advance(1)
    ctl.tick()
    assert ctl.mode == Mode.ALERT


def test_rewake_repeats_hourly_after_next_acknowledgement(ctl, clock):
    ctl.on_containers(A)
    ctl.on_button(SHORT)
    clock.advance(3600)
    ctl.tick()
    ctl.on_button(SHORT)
    clock.advance(3599)
    ctl.tick()
    assert ctl.mode != Mode.ALERT
    clock.advance(1)
    ctl.tick()
    assert ctl.mode == Mode.ALERT


def test_alert_elapsed_counts_from_alert_start(ctl, clock):
    assert ctl.alert_elapsed() == 0.0
    clock.advance(100)
    ctl.on_containers(A)
    clock.advance(60)
    assert ctl.alert_elapsed() == pytest.approx(60)
    ctl.on_button(SHORT)
    assert ctl.alert_elapsed() == 0.0
