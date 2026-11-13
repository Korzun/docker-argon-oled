from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
from luma.core.error import DeviceNotFoundError, DevicePermissionError
from luma.core.render import canvas

from button import ButtonUnavailableError, start_button
from containers import fetch_containers, unhealthy_names
from pages import Snapshot, build_page, draw_page, drift_offset
from state import Controller, Mode
from sysinfo import read_host_info

THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
HOST_ROOT = "/host"
REFRESH_SECONDS = 2
TICK_SECONDS = 0.1
HEALTH_PORT = 8099
HEALTH_MAX_AGE = 5 * REFRESH_SECONDS  # unhealthy if the loop hasn't ticked for this long
I2C_PORT = 1
I2C_ADDRESS = 0x3C
WIDTH = 128
HEIGHT = 64
NORMAL_CONTRAST = 255
ALERT_CONTRAST = 64


def cpu_temp(path=THERMAL_PATH):
    with open(path) as f:
        return int(f.read()) / 1000


def read_stats(thermal_path=THERMAL_PATH, host_root=HOST_ROOT):
    try:
        temp = cpu_temp(thermal_path)
    except (OSError, ValueError):
        temp = None  # shown as n/a; a missing sensor shouldn't kill the display
    return {
        "cpu": psutil.cpu_percent(),
        "temp": temp,
        "mem": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage(host_root).percent,
    }


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={value!r} is not a valid boolean (use true or false)")


def _env_int(environ, name, default):
    value = environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name}={value!r} is not a whole number") from None


@dataclass(frozen=True)
class Settings:
    proxy_url: str = "http://127.0.0.1:2375"
    button_line: int = 4
    button_chip: str | None = None
    health_port: int = HEALTH_PORT

    @classmethod
    def from_env(cls, environ=os.environ) -> "Settings":
        return cls(
            proxy_url=(environ.get("DOCKER_PROXY_URL") or cls.proxy_url).rstrip("/"),
            button_line=_env_int(environ, "BUTTON_GPIO", cls.button_line),
            button_chip=environ.get("BUTTON_CHIP") or None,
            health_port=_env_int(environ, "HEALTH_PORT", cls.health_port),
        )


def make_device(flip=None):
    """Build the display. `flip` rotates it 180 degrees; defaults to $FLIP_DISPLAY."""
    # Imported here so tests can import this module without I2C support.
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    if flip is None:
        flip = env_flag("FLIP_DISPLAY")
    return ssd1306(
        i2c(port=I2C_PORT, address=I2C_ADDRESS),
        width=WIDTH,
        height=HEIGHT,
        rotate=2 if flip else 0,
    )


class Heartbeat:
    """Records when the main loop last completed a tick (lit or dark)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = None

    def beat(self):
        self._last = self._clock()

    def age(self):
        """Seconds since the last beat, or None if there hasn't been one."""
        last = self._last
        return None if last is None else self._clock() - last


def health_status(heartbeat, max_age=HEALTH_MAX_AGE):
    """Return (http_status, body) describing whether the main loop is alive."""
    age = heartbeat.age()
    if age is None:
        return 503, {"status": "starting"}
    if age > max_age:
        return 503, {"status": "stale", "seconds_since_update": round(age, 1)}
    return 200, {"status": "ok", "seconds_since_update": round(age, 1)}


def make_health_server(heartbeat, port=HEALTH_PORT, host="127.0.0.1", max_age=HEALTH_MAX_AGE):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            code, body = health_status(heartbeat, max_age)
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # health checks every few seconds would flood the logs

    return ThreadingHTTPServer((host, port), Handler)


def collect_snapshot(proxy_url: str) -> Snapshot:
    return Snapshot(
        stats=read_stats(),
        containers=fetch_containers(proxy_url),
        host=read_host_info(ip_override=os.environ.get("HOST_IP") or None),
    )


def apply_mode(device, mode: Mode, previous: Mode | None) -> None:
    """Switch the panel on or off and set contrast, only when the mode changes."""
    if mode is previous:
        return
    if mode is Mode.OFF:
        device.hide()
        return
    if previous is None or previous is Mode.OFF:
        device.show()
    device.contrast(ALERT_CONTRAST if mode is Mode.ALERT else NORMAL_CONTRAST)


def main(
    device=None,
    settings=None,
    controller=None,
    events=None,
    heartbeat=None,
    collect=None,
    clock=None,
    sleep=None,
    tick: float = TICK_SECONDS,
    iterations=None,
):
    """Run the display loop forever, or `iterations` ticks if given (used by tests)."""
    collect = collect or collect_snapshot
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    settings = settings or Settings.from_env()
    device = device or make_device()
    controller = controller or Controller(clock=clock)
    events = events if events is not None else queue.Queue()

    snapshot = None
    last_containers = None  # last container list we actually got, for the alert page
    collect_failing = False
    proxy_down = False
    last_refresh = None
    previous_mode = None
    drawn = None
    count = 0
    while iterations is None or count < iterations:
        now = clock()
        refreshed = last_refresh is None or now - last_refresh >= REFRESH_SECONDS
        if refreshed:
            last_refresh = now
            try:
                fresh = collect(settings.proxy_url)
            except Exception as e:  # a transient failure must not end the loop
                if not collect_failing:
                    print(f"WARNING: could not collect status, keeping the last values: {e!r}", file=sys.stderr)
                collect_failing = True
                refreshed = False
            else:
                collect_failing = False
                snapshot = fresh
                if fresh.containers is not None:
                    last_containers = fresh.containers
                    if proxy_down:
                        print("INFO: Docker proxy reachable again", file=sys.stderr)
                    proxy_down = False
                elif not proxy_down:
                    print(
                        f"WARNING: Docker proxy unreachable at {settings.proxy_url}; container info shows "
                        "n/a. Check DOCKER_PROXY_URL and `docker compose logs docker-proxy`.",
                        file=sys.stderr,
                    )
                    proxy_down = True
                controller.on_containers(unhealthy_names(fresh.containers))
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Exception):  # the button thread died
                raise item
            controller.on_button(item)
        controller.tick()

        mode = controller.mode
        apply_mode(device, mode, previous_mode)
        previous_mode = mode
        if mode is Mode.OFF or snapshot is None:
            drawn = None
        else:
            offset = drift_offset(controller.alert_elapsed()) if mode is Mode.ALERT else (0, 0)
            key = (mode, controller.page, offset)
            if refreshed or key != drawn:
                shown = snapshot
                if mode is Mode.ALERT and snapshot.containers is None and last_containers is not None:
                    shown = replace(snapshot, containers=last_containers)  # keep naming the bad containers
                content = build_page(controller.page, shown, alert=mode is Mode.ALERT)
                with canvas(device) as draw:
                    draw_page(draw, content, offset, inverted=mode is not Mode.ALERT)
                drawn = key

        if heartbeat is not None:
            heartbeat.beat()
        count += 1
        sleep(tick)


def _exit_on_sigterm(signum, frame):
    raise SystemExit(0)  # unwinds normally so luma's atexit hook switches the panel off


def cli(settings=None, serve_health=True):
    """Run forever; on display or button failure, explain why on stderr and return 1.

    Serves GET /health on the configured port unless `serve_health` is False.
    """
    try:
        settings = settings or Settings.from_env()
    except ValueError as e:
        print(f"ERROR: invalid setting: {e}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _exit_on_sigterm)  # `docker stop` must not leave a frame lit
    where = f"I2C bus {I2C_PORT}, address {I2C_ADDRESS:#04x}"
    heartbeat = Heartbeat()
    if serve_health:
        try:
            server = make_health_server(heartbeat, port=settings.health_port)
        except OSError as e:
            print(
                f"ERROR: could not start the /health endpoint on 127.0.0.1:{settings.health_port}: {e}\n"
                "Set HEALTH_PORT to a free port.",
                file=sys.stderr,
            )
            return 1
        threading.Thread(target=server.serve_forever, daemon=True).start()
    events = queue.Queue()
    try:
        start_button(settings.button_line, settings.button_chip, events)
        main(settings=settings, events=events, heartbeat=heartbeat)
    except ButtonUnavailableError as e:
        print(
            f"ERROR: could not use the display button (GPIO line {settings.button_line}): {e}\n"
            "Check that the right /dev/gpiochip* is passed into the container "
            "(GPIO_DEVICE) and that no other program, such as Argon's own daemon, "
            "is using the line.",
            file=sys.stderr,
        )
    except DeviceNotFoundError as e:
        print(
            f"ERROR: could not talk to the OLED display ({where}): {e}\n"
            "Check that I2C is enabled on the host (raspi-config), the display "
            "is connected, and /dev/i2c-1 is passed into the container.",
            file=sys.stderr,
        )
    except DevicePermissionError as e:
        print(
            f"ERROR: permission denied accessing the I2C device ({where}): {e}\n"
            "Check that the container is allowed to access /dev/i2c-1.",
            file=sys.stderr,
        )
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(cli())
