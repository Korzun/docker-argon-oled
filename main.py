import sys
import time

import psutil
from luma.core.error import DeviceNotFoundError, DevicePermissionError
from luma.core.render import canvas

THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
HOST_ROOT = "/host"
REFRESH_SECONDS = 2
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


def main(device=None, interval=REFRESH_SECONDS, iterations=None):
    """Draw stats forever, or `iterations` times if given (used by tests)."""
    if device is None:
        device = make_device()
    count = 0
    while iterations is None or count < iterations:
        stats = read_stats()
        with canvas(device) as draw:
            render(draw, stats)
        count += 1
        time.sleep(interval)


def cli():
    """Run forever; on display failure, explain why on stderr and return 1."""
    where = f"I2C bus {I2C_PORT}, address {I2C_ADDRESS:#04x}"
    try:
        main()
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
