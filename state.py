from __future__ import annotations

import enum
import time

PAGE_OVERVIEW, PAGE_CONTAINERS, PAGE_NETWORK, PAGE_SYSTEM = 0, 1, 2, 3
PAGE_COUNT = 4


class ButtonEvent(enum.Enum):
    SHORT = "short"
    LONG = "long"


class Mode(enum.Enum):
    OFF = "off"
    AWAKE = "awake"
    ALERT = "alert"


class Controller:
    """Pure off / awake / alert logic. Time comes from an injected clock."""

    def __init__(self, clock=time.monotonic, wake_timeout: float = 30.0, rewake_after: float = 3600.0):
        self._clock = clock
        self._wake_timeout = wake_timeout
        self._rewake_after = rewake_after
        self._mode = Mode.OFF
        self._page = PAGE_OVERVIEW
        self._deadline: float | None = None
        self._unhealthy: frozenset[str] = frozenset()
        self._acked: frozenset[str] = frozenset()
        self._acked_at: float | None = None
        self._alert_started: float | None = None

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def page(self) -> int:
        return self._page

    def alert_elapsed(self) -> float:
        if self._mode is not Mode.ALERT or self._alert_started is None:
            return 0.0
        return self._clock() - self._alert_started

    def on_button(self, event: ButtonEvent) -> None:
        now = self._clock()
        if self._mode is Mode.ALERT:
            self._acked = self._unhealthy
            self._acked_at = now
            self._alert_started = None
            self._mode = Mode.AWAKE
            self._page = PAGE_CONTAINERS
        elif self._mode is Mode.OFF:
            self._mode = Mode.AWAKE
            self._page = PAGE_OVERVIEW
        elif event is ButtonEvent.LONG:
            self._page = PAGE_OVERVIEW
        else:
            self._page = (self._page + 1) % PAGE_COUNT
        self._deadline = now + self._wake_timeout

    def on_containers(self, unhealthy: frozenset[str] | None) -> None:
        if unhealthy is None:  # unknown is not "recovered", and not a reason to wake
            return
        self._unhealthy = unhealthy
        self._acked = self._acked & unhealthy
        if not self._acked:
            self._acked_at = None
        if self._mode is Mode.ALERT and not unhealthy:
            self._mode = Mode.OFF
            self._alert_started = None
            self._deadline = None
        elif unhealthy - self._acked and self._mode is not Mode.ALERT:
            self._enter_alert()

    def tick(self) -> None:
        now = self._clock()
        if self._mode is Mode.AWAKE and self._deadline is not None and now >= self._deadline:
            self._mode = Mode.OFF
            self._deadline = None
        if (
            self._mode is not Mode.ALERT
            and self._acked
            and self._acked_at is not None
            and now - self._acked_at >= self._rewake_after
        ):
            self._acked = frozenset()
            self._acked_at = None
            self._enter_alert()

    def _enter_alert(self) -> None:
        self._mode = Mode.ALERT
        self._page = PAGE_CONTAINERS
        self._alert_started = self._clock()
        self._deadline = None
