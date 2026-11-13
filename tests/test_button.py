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

    def __init__(self, clock, script, stop, line_pressed=None):
        self.clock, self.script, self.stop = clock, list(script), stop
        self.line_pressed = line_pressed  # what is_pressed() reports; None = source has no such method

    def wait_edge(self, timeout):
        if not self.script:
            self.stop.set()
            return None
        dt, edge = self.script.pop(0)
        self.clock.now += dt
        return None if edge is None else (edge, self.clock.now)

    def is_pressed(self):
        return self.line_pressed


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
            return (self.steps.pop(0), time.monotonic())

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


# --- event timestamps -----------------------------------------------------


def test_classifier_uses_event_timestamps_not_processing_time(clf, clock):
    clock.now = 99.0  # both edges are processed late and at the same instant
    assert clf.on_edge(True, 10.0) is None
    assert clf.on_edge(False, 10.08) == SHORT


def test_without_timestamps_a_batched_tap_is_dropped_as_bounce(clf, clock):
    clock.now = 99.0
    clf.on_edge(True)
    assert clf.on_edge(False) is None  # why event timestamps matter


# --- resync with the real line state --------------------------------------


def test_pump_synthesizes_a_release_when_the_line_is_already_released(clock):
    stop = threading.Event()
    out = queue.Queue()
    source = FakeSource(clock, [(0.0, True), (0.2, None)], stop, line_pressed=False)
    pump_events(source, PressClassifier(clock=clock), out, stop)
    assert out.get_nowait() == SHORT  # the rising edge never arrived


def test_pump_does_not_resync_while_the_line_is_still_pressed(clock):
    stop = threading.Event()
    out = queue.Queue()
    source = FakeSource(clock, [(0.0, True), (0.2, None), (0.2, None)], stop, line_pressed=True)
    pump_events(source, PressClassifier(clock=clock), out, stop)
    assert out.empty()


def test_pump_works_with_a_source_that_has_no_is_pressed(clock):
    class Bare:
        def __init__(self):
            self.n = 0

        def wait_edge(self, timeout):
            self.n += 1
            if self.n == 1:
                return (True, clock.now)
            stop.set()
            return None

    stop = threading.Event()
    out = queue.Queue()
    pump_events(Bare(), PressClassifier(clock=clock), out, stop)
    assert out.empty()


# --- GpiodEdgeSource against a fake gpiod module --------------------------


class _Enum:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


class FakeEvent:
    class Type:
        FALLING_EDGE = _Enum("FALLING")
        RISING_EDGE = _Enum("RISING")

    def __init__(self, falling, ns):
        self.event_type = self.Type.FALLING_EDGE if falling else self.Type.RISING_EDGE
        self.timestamp_ns = ns


class FakeRequest:
    def __init__(self):
        self.waits = 0
        self.pending = []
        self.value = None

    def wait_edge_events(self, timeout=None):
        self.waits += 1
        return bool(self.pending)

    def read_edge_events(self):
        events, self.pending = self.pending, []
        return events

    def get_value(self, line):
        return self.value


@pytest.fixture
def fake_gpiod(monkeypatch, tmp_path):
    import types

    module = types.ModuleType("gpiod")
    line = types.ModuleType("gpiod.line")
    line.Bias = types.SimpleNamespace(PULL_UP=_Enum("PULL_UP"))
    line.Direction = types.SimpleNamespace(INPUT=_Enum("INPUT"))
    line.Edge = types.SimpleNamespace(BOTH=_Enum("BOTH"))
    line.Value = types.SimpleNamespace(ACTIVE=_Enum("ACTIVE"), INACTIVE=_Enum("INACTIVE"))
    module.line = line
    module.LineSettings = lambda **kw: kw
    module.request = FakeRequest()
    module.calls = []
    module.error = None

    def request_lines(path, consumer, config):
        module.calls.append((path, consumer, config))
        if module.error:
            raise module.error
        return module.request

    module.request_lines = request_lines
    monkeypatch.setitem(sys.modules, "gpiod", module)
    monkeypatch.setitem(sys.modules, "gpiod.line", line)
    chip = tmp_path / "gpiochip0"
    chip.write_text("")
    module.chip = str(chip)
    return module


def test_gpiod_source_requests_the_line_with_pull_up_and_both_edges(fake_gpiod):
    button.GpiodEdgeSource(4, fake_gpiod.chip)
    path, consumer, config = fake_gpiod.calls[0]
    assert path == fake_gpiod.chip and consumer == "oled-button"
    assert list(config) == [4]
    assert config[4]["bias"].name == "PULL_UP" and config[4]["edge_detection"].name == "BOTH"


def test_gpiod_source_returns_edges_with_timestamps_and_buffers_batches(fake_gpiod):
    source = button.GpiodEdgeSource(4, fake_gpiod.chip)
    fake_gpiod.request.pending = [FakeEvent(True, 2_000_000_000), FakeEvent(False, 2_090_000_000)]
    assert source.wait_edge(0.05) == (True, 2.0)
    waits = fake_gpiod.request.waits
    assert source.wait_edge(0.05) == (False, pytest.approx(2.09))
    assert fake_gpiod.request.waits == waits  # second edge came from the buffer
    assert source.wait_edge(0.05) is None  # nothing left, timeout


def test_gpiod_source_is_pressed_reads_the_line(fake_gpiod):
    source = button.GpiodEdgeSource(4, fake_gpiod.chip)
    fake_gpiod.request.value = sys.modules["gpiod.line"].Value.INACTIVE  # physical low = pressed
    assert source.is_pressed() is True
    fake_gpiod.request.value = sys.modules["gpiod.line"].Value.ACTIVE
    assert source.is_pressed() is False


def test_gpiod_source_permission_error_is_explained(fake_gpiod):
    fake_gpiod.error = PermissionError(13, "Permission denied")
    with pytest.raises(ButtonUnavailableError, match="permission denied"):
        button.GpiodEdgeSource(4, fake_gpiod.chip)


def test_gpiod_source_busy_line_is_explained(fake_gpiod):
    fake_gpiod.error = OSError(16, "Device or resource busy")
    with pytest.raises(ButtonUnavailableError) as exc:
        button.GpiodEdgeSource(4, fake_gpiod.chip)
    assert "GPIO line 4" in str(exc.value) and "busy" in str(exc.value)
