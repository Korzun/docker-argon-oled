import os
import queue
import subprocess
import sys
import threading
import time

import pytest

import button
from button import ButtonUnavailableError, PressClassifier, pump_events
from state import ButtonEvent

SHORT, LONG = ButtonEvent.SHORT, ButtonEvent.LONG


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def clf(clock):
    return PressClassifier(clock=clock)


def replay(clf, clock, edges):
    """edges: (timestamp, pressed) pairs; returns the events produced."""
    events = []
    for t, pressed in edges:
        clock.now = t
        ev = clf.on_edge(pressed)
        if ev:
            events.append(ev)
    return events


def test_short_press_fires_on_release(clf, clock):
    clock.now = 10.0
    assert clf.on_edge(True) is None
    clock.now = 10.1
    assert clf.on_edge(False) == SHORT


def test_long_press_fires_once_while_held_and_not_on_release(clf, clock):
    clock.now = 10.0
    clf.on_edge(True)
    clock.now = 10.99
    assert clf.poll() is None
    clock.now = 11.0
    assert clf.poll() == LONG
    clock.now = 11.5
    assert clf.poll() is None
    clock.now = 12.0
    assert clf.on_edge(False) is None


def test_long_hold_released_without_poll_still_reports_long(clf, clock):
    clock.now = 10.0
    clf.on_edge(True)
    clock.now = 11.5
    assert clf.on_edge(False) == LONG


def test_bounce_shorter_than_debounce_ignored(clf, clock):
    clock.now = 10.0
    clf.on_edge(True)
    clock.now = 10.001
    assert clf.on_edge(False) is None


def test_duplicate_press_edges_ignored(clf, clock):
    clock.now = 10.0
    clf.on_edge(True)
    clock.now = 10.05
    assert clf.on_edge(True) is None
    clock.now = 10.1
    assert clf.on_edge(False) == SHORT  # measured from the first press edge


def test_release_without_press_ignored(clf, clock):
    clock.now = 10.0
    assert clf.on_edge(False) is None


def test_hold_across_poll_boundaries(clf, clock):
    clock.now = 5.0
    clf.on_edge(True)
    produced = []
    for step in range(1, 30):
        clock.now = 5.0 + step * 0.1
        ev = clf.poll()
        if ev:
            produced.append((round(clock.now - 5.0, 1), ev))
    assert produced == [(1.0, LONG)]


# Real captures from the Pi (gpiomon, GPIO 4, falling = pressed, rising = released).
BOUNCE_CLUSTER = [
    (20742.961454523, True), (20743.075284171, False),
    (20743.160162488, True), (20743.267766590, True), (20743.267917441, True),
    (20743.267978348, False), (20743.268050163, True), (20743.268133236, False),
    (20743.327986352, True), (20743.413226927, False),
]
RAPID_RETAP = [
    (20739.729956340, True), (20739.826930472, False),
    (20739.845374907, True), (20739.930580981, False),
]


def test_recorded_bounce_cluster_yields_three_short_presses(clf, clock):
    assert replay(clf, clock, BOUNCE_CLUSTER) == [SHORT, SHORT, SHORT]


def test_recorded_rapid_retap_counts_two_presses(clf, clock):
    assert replay(clf, clock, RAPID_RETAP) == [SHORT, SHORT]


def test_recorded_long_hold_fires_one_long_and_no_short(clf, clock):
    start, end = 20741.391551825, 20742.797785750
    clock.now = start
    events = []
    if (ev := clf.on_edge(True)):
        events.append(ev)
    t = start
    while t < end:
        t += 0.05
        clock.now = min(t, end)
        if (ev := clf.poll()):
            events.append(ev)
    clock.now = end
    if (ev := clf.on_edge(False)):
        events.append(ev)
    assert events == [LONG]


class FakeSource:
    """Scripted EdgeSource: each entry is (seconds_to_advance, edge)."""

    def __init__(self, clock, script, stop):
        self.clock, self.script, self.stop = clock, list(script), stop

    def wait_edge(self, timeout):
        if not self.script:
            self.stop.set()
            return None
        dt, edge = self.script.pop(0)
        self.clock.now += dt
        return edge


def test_pump_events_puts_classified_events_on_queue(clock):
    stop = threading.Event()
    out = queue.Queue()
    source = FakeSource(
        clock,
        [(0.0, True), (0.1, False), (0.2, None), (0.0, True), (1.2, None), (0.1, False)],
        stop,
    )
    pump_events(source, PressClassifier(clock=clock), out, stop)
    assert [out.get_nowait() for _ in range(out.qsize())] == [SHORT, LONG]


def test_start_button_pumps_events_with_a_source(monkeypatch):
    stop = threading.Event()
    out = queue.Queue()

    class Source:
        def __init__(self, line, chip=None):
            self.steps = [True, False]

        def wait_edge(self, timeout):
            if not self.steps:
                time.sleep(timeout)
                return None
            time.sleep(0.06)
            return self.steps.pop(0)

    monkeypatch.setattr(button, "GpiodEdgeSource", Source)
    thread = button.start_button(4, None, out, stop=stop)
    try:
        assert out.get(timeout=3) == SHORT
    finally:
        stop.set()
        thread.join(timeout=2)


def test_gpiod_import_is_lazy():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.modules['gpiod'] = None; "  # makes `import gpiod` raise ImportError
        f"sys.path.insert(0, {repo!r}); import button; print('ok')"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_gpiod_source_missing_device_raises_unavailable(tmp_path):
    missing = str(tmp_path / "gpiochip9")
    with pytest.raises(ButtonUnavailableError, match="gpiochip9"):
        button.GpiodEdgeSource(4, missing)


def test_gpiod_source_unusable_device_raises_unavailable(tmp_path):
    not_a_chip = tmp_path / "gpiochip0"
    not_a_chip.write_text("")
    with pytest.raises(ButtonUnavailableError):
        button.GpiodEdgeSource(4, str(not_a_chip))


def test_auto_chip_detection_with_no_chip_present_raises_unavailable(monkeypatch):
    monkeypatch.setattr(button, "CHIP_CANDIDATES", ("/nonexistent/gpiochip4", "/nonexistent/gpiochip0"))
    with pytest.raises(ButtonUnavailableError, match="gpiochip"):
        button.GpiodEdgeSource(4, None)


def test_button_thread_failure_is_reported_on_the_queue(monkeypatch):
    stop = threading.Event()
    out = queue.Queue()

    class Dying:
        def __init__(self, line, chip=None):
            pass

        def wait_edge(self, timeout):
            raise OSError("device went away")

    monkeypatch.setattr(button, "GpiodEdgeSource", Dying)
    thread = button.start_button(4, None, out, stop=stop)
    try:
        err = out.get(timeout=3)
        assert isinstance(err, ButtonUnavailableError)
        assert "device went away" in str(err)
    finally:
        stop.set()
        thread.join(timeout=2)
