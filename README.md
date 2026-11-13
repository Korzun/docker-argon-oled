# docker-argon-oled

A Docker-based controller for the Argon OLED module on a Raspberry Pi (Argon ONE V5, SSD1306 128x64). The screen is **dark by default** to protect the panel. Press the OLED button to wake it and step through status pages. If a container becomes unhealthy, the screen wakes by itself and stays lit until you press the button.

## Pages

Press the button to wake the screen to page 1; each further press moves to the next page and wraps around. Pages never cycle on their own. After 30 seconds with no press the screen turns off. A long press (about 1 second) jumps back to page 1.

```
ALL OK            1/4     CONTAINERS       2/4
Temp    45.6C             coolify          ok
Uptime  12d 3h            oled              -
Ctrs    7 up, 0 bad       postgres         ok
IP      192.168.1.42      +4 more

NETWORK           3/4     SYSTEM           4/4
pi-basement               CPU   12.3%
192.168.1.42              Mem   34.5%
                          Disk  56.7%
                          Temp  45.6C
```

Container statuses: `ok` (healthy), `bad` (unhealthy), `..` (health check starting), `-` (running, no health check), `stopped`.

**Alerts.** When a container's Docker health status becomes `unhealthy`, the screen wakes on its own, jumps to the Containers page (title `! UNHEALTHY`) and stays lit, at reduced contrast, with the content drifting a pixel every minute and a plain title (a thin rule instead of the filled bar, so far fewer pixels are lit) so the panel doesn't burn in. Press the button to acknowledge: it returns to normal paging and goes dark after 30 seconds. If the problem is still there an hour later it wakes again; a new or re-failing container wakes it immediately. If every unhealthy container recovers on its own, the screen simply turns off.

## Requirements

- A Raspberry Pi 5 with the Argon ONE V5 case and OLED module (SSD1306, I2C address `0x3C`)
- I2C enabled on the host (`sudo raspi-config` → Interface Options → I2C), so that `/dev/i2c-1` exists
- Docker with the Compose plugin

## Usage

```sh
docker compose up -d --build
```

The container restarts automatically unless stopped (`restart: unless-stopped`). To stop it: `docker compose down`.

## How it works

- `main.py` runs a 0.1 s loop that wires everything together. The other modules each do one job: `state.py` (off / awake / alert logic), `pages.py` (page content and drawing), `button.py` (GPIO button input), `containers.py` (container health), `sysinfo.py` (hostname, IP, uptime).
- The display is driven with [luma.oled](https://github.com/rm-hull/luma.oled); stats come from [psutil](https://github.com/giampaolo/psutil). The button is read with [libgpiod](https://git.kernel.org/pub/scm/libs/libgpiod/libgpiod.git/) edge events: GPIO line 4 on `/dev/gpiochip0`, active-low.
- **Container health goes through a read-only proxy.** `docker-compose.yml` runs [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) with only the containers endpoints enabled and writes disabled. It is the only service that mounts the Docker socket, and it listens on `127.0.0.1:2375` only. The display app never has control of Docker. Know the trade-off: the proxy's `CONTAINERS=1` setting allows read-only requests to every `/containers/*` endpoint, including a container's environment (`/containers/<id>/json`) and logs, and port 2375 is reachable by any process on the Pi. If other users or untrusted host-network containers run there, swap in a proxy that can allow only the list endpoint. (Mounting the socket read-only into the app would not help: `:ro` doesn't limit what the Docker API allows.)
- The `oled` service uses **host networking** so it can show the Pi's real LAN IP and reach the proxy on `127.0.0.1`. It also mounts the host root read-only at `/host` so disk usage and the hostname reflect the host.

## Configuration

| Variable          | Default            | Description |
| ----------------- | ------------------ | ----------- |
| `FLIP_DISPLAY`    | `false`            | Set to `true` to rotate the display 180° (for a panel mounted upside-down). Accepts `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`. |
| `GPIO_DEVICE`     | `/dev/gpiochip0`   | GPIO chip device passed into the container and used for the button. On a Pi 5, `gpiodetect` shows the header chip as `pinctrl-rp1`. |
| `BUTTON_GPIO`     | `4`                | GPIO line of the OLED button on that chip (active-low). |
| `HEALTH_PORT`     | `8099`             | Port for the `/health` endpoint, bound to `127.0.0.1`. |
| `PROXY_PORT`      | `2375`             | Host loopback port the Docker proxy is published on, and where the app looks for it. |
| `HOST_IP`         | (auto-detected)    | Override the IP address shown, if auto-detection picks the wrong one. |

Set variables in a `.env` file next to `docker-compose.yml`, in your shell, or in Coolify's Environment Variables, then redeploy (a restart doesn't pick them up). Other settings, such as the wake timeout, I2C address or display size, are constants in the code.

## Development

Tests run without a Pi, I2C or GPIO hardware, using luma's in-memory `dummy` display, fake clocks and fake button edges:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

CI (GitHub Actions) runs the tests, validates `docker-compose.yml`, and builds the image on every push and pull request. Things only a real Pi can confirm: that the button is on the line above, how the panel looks, and that the container can open the GPIO and I2C devices.

## Health check

The app serves `GET /health` on `127.0.0.1:${HEALTH_PORT}`. It returns `200` while the main loop is running and `503` if the loop hasn't ticked for 10 seconds or hasn't started yet. A dark screen is normal and still reports healthy:

```json
{"status": "ok", "seconds_since_update": 0.1}
```

`docker-compose.yml` defines a matching Docker `healthcheck`, which Docker and Coolify use to report the container as healthy or unhealthy. It mainly catches a hung loop. If the display or button is unavailable, the app exits with an explanation instead (see Troubleshooting).

## Troubleshooting

If the display can't be reached (I2C disabled, OLED not connected, or no permission for `/dev/i2c-1`), or the button's GPIO line can't be opened (wrong `GPIO_DEVICE`, the chip not passed in, or another program such as Argon's own daemon using the line), the container prints an explanation to its logs and exits with a non-zero status. `restart: unless-stopped` makes Docker retry with an increasing delay, so it recovers on its own once the problem is fixed. Check the reason with:

```sh
docker compose logs oled
```

If I2C is disabled on the host, or `GPIO_DEVICE` names a device that doesn't exist, `docker compose up` may fail before the container starts, because Docker can't map a missing device.

If the Containers page says `Docker n/a`, the proxy isn't reachable; the app logs a warning with the URL it tried (and a note when it comes back), and `docker compose logs docker-proxy` shows the proxy's side. If `/health` can't start because its port is taken, the app says so and exits; set `HEALTH_PORT` to a free port. A missing CPU temperature sensor isn't fatal; the temperature shows `n/a`.
