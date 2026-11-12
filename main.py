import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
from luma.core.error import DeviceNotFoundError, DevicePermissionError
from luma.core.render import canvas

THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
HOST_ROOT = "/host"
REFRESH_SECONDS = 2
HEALTH_PORT = 8080
HEALTH_MAX_AGE = 5 * REFRESH_SECONDS  # unhealthy if no redraw for this long
I2C_PORT = 1
I2C_ADDRESS = 0x3C
WIDTH = 128
HEIGHT = 64


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


def render(draw, stats):
    draw.text((0, 0), f"CPU  {stats['cpu']:5.1f}%", fill="white")
    temp = "  n/a" if stats["temp"] is None else f"{stats['temp']:5.1f}C"
    draw.text((0, 14), f"Temp {temp}", fill="white")
    draw.text((0, 28), f"Mem  {stats['mem']:5.1f}%", fill="white")
    draw.text((0, 42), f"Disk {stats['disk']:5.1f}%", fill="white")


def make_device():
    # Imported here so tests can import this module without I2C support.
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    return ssd1306(i2c(port=I2C_PORT, address=I2C_ADDRESS), width=WIDTH, height=HEIGHT)


class Heartbeat:
    """Records when the display loop last completed a successful redraw."""

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
    """Return (http_status, body) describing whether the display loop is alive."""
    age = heartbeat.age()
    if age is None:
        return 503, {"status": "starting"}
    if age > max_age:
        return 503, {"status": "stale", "seconds_since_update": round(age, 1)}
    return 200, {"status": "ok", "seconds_since_update": round(age, 1)}


def make_health_server(heartbeat, port=HEALTH_PORT, host="0.0.0.0", max_age=HEALTH_MAX_AGE):
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


def main(device=None, interval=REFRESH_SECONDS, iterations=None, heartbeat=None):
    """Draw stats forever, or `iterations` times if given (used by tests)."""
    if device is None:
        device = make_device()
    count = 0
    while iterations is None or count < iterations:
        stats = read_stats()
        with canvas(device) as draw:
            render(draw, stats)
        if heartbeat is not None:
            heartbeat.beat()
        count += 1
        time.sleep(interval)


def cli(health_port=HEALTH_PORT):
    """Run forever; on display failure, explain why on stderr and return 1.

    Serves GET /health on `health_port` (None disables it).
    """
    where = f"I2C bus {I2C_PORT}, address {I2C_ADDRESS:#04x}"
    heartbeat = Heartbeat()
    if health_port is not None:
        server = make_health_server(heartbeat, port=health_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        main(heartbeat=heartbeat)
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
