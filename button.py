from __future__ import annotations

import collections
import os
import queue
import threading
import time

from state import ButtonEvent

CHIP_CANDIDATES = ("/dev/gpiochip4", "/dev/gpiochip0")
POLL_SECONDS = 0.05


class ButtonUnavailableError(Exception):
    """The button's GPIO line can't be used; the message says why in plain words."""


class PressClassifier:
    """Turns raw press/release edges into SHORT and LONG events.

    Debounce is judged on how long the button was held, not on the gap between
    presses, so a fast re-tap after a release still counts as a new press.
    """

    def __init__(self, long_press_s: float = 1.0, debounce_s: float = 0.03, clock=time.monotonic):
        self._long = long_press_s
        self._debounce = debounce_s
        self._clock = clock
        self._pressed_at: float | None = None
        self._long_fired = False

    @property
    def is_held(self) -> bool:
        return self._pressed_at is not None

    def on_edge(self, pressed: bool, at: float | None = None) -> ButtonEvent | None:
        """`at` is when the edge really happened (monotonic seconds); default is now."""
        now = self._clock() if at is None else at
        if pressed:
            if self._pressed_at is None:
                self._pressed_at = now
                self._long_fired = False
            return None
        if self._pressed_at is None:
            return None
        held = now - self._pressed_at
        was_long = self._long_fired
        self._pressed_at = None
        self._long_fired = False
        if was_long or held < self._debounce:
            return None
        return ButtonEvent.LONG if held >= self._long else ButtonEvent.SHORT

    def poll(self) -> ButtonEvent | None:
        if self._pressed_at is None or self._long_fired:
            return None
        if self._clock() - self._pressed_at >= self._long:
            self._long_fired = True
            return ButtonEvent.LONG
        return None


def pump_events(source, classifier: PressClassifier, out: queue.Queue, stop: threading.Event) -> None:
    """Read edges from `source` until `stop` is set, putting classified events on `out`.

    `source.wait_edge(timeout)` returns `(pressed, timestamp)` or None on timeout. If the
    source also has `is_pressed()`, a release whose edge never arrived is detected from the
    real line state.
    """
    while not stop.is_set():
        edge = source.wait_edge(POLL_SECONDS)
        if edge is not None:
            pressed, at = edge
            event = classifier.on_edge(pressed, at)
        else:
            event = classifier.poll()
            if event is None and classifier.is_held:
                is_pressed = getattr(source, "is_pressed", None)
                if is_pressed is not None and not is_pressed():
                    event = classifier.on_edge(False)
        if event is not None:
            out.put(event)


class GpiodEdgeSource:
    """Edge events from a GPIO line via libgpiod (falling edge = pressed, internal pull-up)."""

    def __init__(self, line: int, chip: str | None = None):
        path = self._resolve_chip(chip)
        try:
            import gpiod
            from gpiod.line import Bias, Direction, Edge, Value
        except ImportError as e:
            raise ButtonUnavailableError(f"the gpiod library is not installed ({e})") from e
        try:
            self._request = gpiod.request_lines(
                path,
                consumer="oled-button",
                config={
                    line: gpiod.LineSettings(
                        direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_UP
                    )
                },
            )
        except PermissionError as e:
            raise ButtonUnavailableError(f"permission denied opening {path}: {e}") from e
        except OSError as e:
            raise ButtonUnavailableError(
                f"could not use GPIO line {line} on {path}: {e}. If the line is busy, another "
                "program (such as Argon's own daemon) may be using it."
            ) from e
        self._line = line
        self._pressed_value = Value.INACTIVE  # pull-up: physical low (pressed) reads INACTIVE
        self._buffer: collections.deque = collections.deque()

    @staticmethod
    def _resolve_chip(chip: str | None) -> str:
        if chip:
            if not os.path.exists(chip):
                raise ButtonUnavailableError(
                    f"GPIO device {chip} does not exist; pass the right /dev/gpiochip* into the container"
                )
            return chip
        for candidate in CHIP_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        raise ButtonUnavailableError(
            "no /dev/gpiochip* device found; pass the header GPIO chip into the container"
        )

    def wait_edge(self, timeout: float) -> tuple[bool, float] | None:
        """Next edge as (pressed, monotonic seconds when it happened), or None on timeout."""
        if not self._buffer:
            if not self._request.wait_edge_events(timeout=timeout):
                return None
            for event in self._request.read_edge_events():
                pressed = event.event_type == event.Type.FALLING_EDGE
                self._buffer.append((pressed, event.timestamp_ns / 1e9))
        return self._buffer.popleft() if self._buffer else None

    def is_pressed(self) -> bool:
        return self._request.get_value(self._line) == self._pressed_value


def start_button(
    line: int, chip: str | None, out: queue.Queue, stop: threading.Event | None = None
) -> threading.Thread:
    """Open the button line (raises ButtonUnavailableError) and pump events on a daemon thread."""
    source = GpiodEdgeSource(line, chip)
    stop = stop or threading.Event()

    def run() -> None:
        try:
            pump_events(source, PressClassifier(), out, stop)
        except Exception as e:  # tell the main loop; a dead button must not fail silently
            out.put(ButtonUnavailableError(f"button input stopped: {e}"))

    thread = threading.Thread(target=run, daemon=True, name="button")
    thread.start()
    return thread
