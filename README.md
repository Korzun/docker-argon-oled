# docker-argon-oled

A minimal Docker-based controller for the Argon OLED display on a Raspberry Pi. It shows live system stats on the SSD1306 128x64 screen, refreshed every 2 seconds:

```
CPU   12.3%
Temp  45.6C
Mem   34.5%
Disk  56.7%
```

## Requirements

- A Raspberry Pi with an Argon case and OLED (SSD1306, I2C address `0x3C`)
- I2C enabled on the host (`sudo raspi-config` → Interface Options → I2C), so that `/dev/i2c-1` exists
- Docker with the Compose plugin

## Usage

```sh
docker compose up -d --build
```

The container restarts automatically unless stopped (`restart: unless-stopped`).

To stop it:

```sh
docker compose down
```

## How it works

- `main.py` drives the display with [luma.oled](https://github.com/rm-hull/luma.oled) and reads stats with [psutil](https://github.com/giampaolo/psutil). CPU temperature comes from `/sys/class/thermal/thermal_zone0/temp`.
- `dockerfile` builds a `python:3.12-slim` image with the dependencies installed.
- `docker-compose.yml` passes `/dev/i2c-1` into the container and mounts the host root filesystem read-only at `/host`, so the disk figure reflects the host rather than the container.

## Configuration

There are no runtime options yet. To change the I2C port or address, the display size, or the refresh interval, edit the constants in `main.py` and rebuild.

## Development

Tests run without a Pi or I2C hardware, using luma's in-memory `dummy` display:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

CI (GitHub Actions) runs the tests and a Docker build on every push and pull request. Hardware-specific details (I2C address, `/dev/i2c-1` passthrough, how the panel looks) can only be checked on a real Pi.
