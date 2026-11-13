# OLED status pages, button, and alerts: design

## Intent

The Pi (Argon ONE V5 case, Argon V5 Industria OLED module) lives in a basement utility room and is looked at rarely. Today the display shows four fixed lines of stats, permanently lit. That is the worst case for an OLED (static content, 24/7, rarely seen) and it doesn't answer the question that matters when someone does walk down: "is everything okay?"

Make the display a glanceable status board that is dark by default, wakes on a button press, pages through a few screens, and demands attention on its own when a container becomes unhealthy.

## Goals

- Screen is **off (dark) by default**. This is the main burn-in defence.
- A press of the OLED module's button wakes the screen to page 1. Each further press advances to the next page, wrapping around. Pages do **not** auto-cycle.
- After 30 s with no press, the screen turns off again.
- If any container becomes **unhealthy**, the screen wakes by itself, jumps to the Containers page, and **stays lit until the button is pressed** (acknowledge).
- Show the data the owner asked for: temperature, uptime, IP address, container health, system stats.
- Keep the existing quality bar: tests that run without hardware, CI green, `/health` endpoint, env-var configuration, Coolify-friendly `docker-compose.yml`.

## Non-goals

- No Coolify API integration or Coolify metrics page (dropped on purpose; container health from Docker covers the need). No Coolify API token.
- No auto-cycling slideshow, no animations, no network-facing UI beyond the existing `/health`.
- Not handling the case's power button (on the V5 it is a plastic piece that physically presses the Pi's own power button; nothing for software to do).
- No reboot/shutdown actions from the display button.

## Anti-goals (would be a failure even if it "works")

- The panel is left lit for long stretches with static content other than during an unacknowledged alert, and the alert case has the burn-in mitigations below.
- The display app gets write access to Docker (see Docker access).
- Using or keeping Argon's own scripts. The owner downloaded `argon1v5-2.sh` to study it; it is untracked and **must not be committed, copied from, or shipped**. Delete it when the work is done. Stage files by name, not `git add -A`.

## Constraints and facts

- Hardware: SSD1306, 128x64, I2C bus 1, address 0x3C (already working through luma.oled). Pi 5-class host running Coolify; 64-bit OS.
- The OLED module's button is **confirmed on GPIO 4 of `/dev/gpiochip0`** (the `pinctrl-rp1` header chip; this kernel has no `gpiochip4`), measured on the real Pi with `gpiomon` 2.2.1. It is active-low: the line sits high and goes low while the button is held. Observed: normal taps last about 70-130 ms; contact bounce produces edge clusters within about 0.2 ms and duplicate falling edges with no rising edge between them; a fast re-tap can start 18 ms after a release. Debounce must therefore judge hold duration, not the gap between presses. Keep the line number and chip configurable.
- The app runs in a container deployed by Coolify from this repo's `docker-compose.yml`. The container will need the GPIO character device passed through, like `/dev/i2c-1`.
- Existing code (`main.py`, `tests/`, `.github/workflows/ci.yml`) is the base: `read_stats`, `render`, `make_device`, `main`, `cli`, `Heartbeat` + `/health`, `FLIP_DISPLAY`, graceful exit with an explanation when the display is missing. Build on it; keep the tests and CI passing.
- The existing Docker `healthcheck` / `/health` endpoint must keep reporting loop liveness **even while the screen is off**. A dark screen is normal and must not look like a hung loop.
- The container's own network view is Docker's, not the Pi's. Showing the Pi's LAN IP and hostname (not the container's) needs a deliberate approach.

## Decided behaviour

**Pages** (in order). Style A was chosen from mockups: an inverted title bar containing the page title and `n/4`, then plain rows of text below it.

```
 ALL OK            1/4        CONTAINERS       2/4
 Temp    45.6C                coolify          ok
 Uptime  12d 3h               oled             ok
 Ctrs    7/7 healthy          postgres         ok
 IP      192.168.1.42         +4 more

 NETWORK           3/4        SYSTEM           4/4
 hostname                     CPU   12.3%
 192.168.1.42                 Mem   34.5%
                              Disk  56.7%
                              Temp  45.6C

 ! UNHEALTHY       2/4
 postgres
 coolify-redis

 press = acknowledge
```

(The mockups were approximations; the exact text, spacing and fonts are the builder's to fit to 128x64.)

1. **Overview**: overall status in the title (`ALL OK` or the alert state), temperature, uptime, container summary, IP.
2. **Containers**: container names with health/status, `+N more` when they don't fit.
3. **Network**: hostname and IP.
4. **System**: CPU, memory, disk, temperature (what the screen shows today).

**Alert behaviour** (decided by the owner): unhealthy container wakes the screen on its own, goes to the Containers page showing which, stays lit until the button is pressed. A new unhealthy container while already acknowledged wakes it again immediately. If every unhealthy container recovers on its own while the alert is lit and unacknowledged, the screen **turns off by itself** (the owner chose panel life over leaving a trace of a self-healed problem); the next press then just wakes to page 1 as usual.

**Docker access** (decided by the owner): read-only via a small socket-proxy container (for example Tecnativa's `docker-socket-proxy`, containers endpoint only, writes disabled). The display app talks to the proxy over the compose network. It never mounts the Docker socket itself. Mounting the socket `:ro` does not limit API access, so that is not an acceptable substitute.

## Calls I made (the owner has not confirmed these)

- **Wake timeout is 30 s** of no presses.
- **First press while dark only wakes** to page 1; it does not also advance.
- **Acknowledging an alert** returns to normal mode on the Containers page, which then times out after 30 s like any wake.
- **Re-wake for a persistent problem:** if an acknowledged problem is still there after about an hour, wake again.
- **Burn-in mitigation during a lit alert:** reduced contrast and a 1-2 px drift of the content every minute or so. Normal 30 s wakes need no mitigation.
- **Long press** (about 1 s) jumps back to page 1.
- **What counts as an alert:** only Docker health status `unhealthy`. Containers without a healthcheck, stopped, or restarting containers appear on the Containers page but do not trigger the alert.
- **If the Docker proxy is unreachable:** the Containers/Overview pages show `n/a` for container info; no alert, no crash.
- Pages: Network shows hostname + IP; Overview includes IP too (as in the mockup).
- **Button probing is done** (see Constraints). Implementation still keeps the line and chip configurable (defaults: line 4, `/dev/gpiochip0`, active-low with internal pull-up; if no chip is given, try `gpiochip4` then `gpiochip0`). Button input uses edge events so a press is never missed while the main loop sleeps.
- **Button event model:** a short press acts on release (so it can be told apart from a long press); a long press fires once at about 1 s while still held.
- **Display off** uses luma's `hide()` (SSD1306 display-off command), not a black frame, so pixels are truly off; contrast is lowered for the alert via the driver's contrast setting. The main loop keeps ticking while dark (stats, container polling, heartbeat for `/health`); it just stops drawing. `FLIP_DISPLAY` is unaffected.
- **Showing the Pi's real LAN IP:** the `oled` service uses `network_mode: host`. The socket proxy publishes its port on `127.0.0.1` only, and the app reaches it there. The `/health` server binds to `127.0.0.1` and its port becomes configurable (`HEALTH_PORT`) with a default that avoids common clashes (Coolify's proxy dashboard commonly uses 8080), and the compose `healthcheck` follows it. If Coolify rejects host networking for this stack, fall back to a `HOST_IP` env var override and read the hostname from `/host/etc/hostname`.
- **Socket proxy lives in this repo's `docker-compose.yml`**, deployed in the same stack: `CONTAINERS=1` only, writes disabled, socket mounted read-only into the proxy only, image version pinned and checked for arm64 support. The app's Docker URL comes from an env var.
- **Which containers count:** every container the proxy can see, including this stack's own. Overview shows running containers; the summary line reads like `7 up, 0 bad` rather than claiming "healthy", because containers without a healthcheck can't be judged. Containers page statuses: `ok` (healthy), `bad` (unhealthy), `..` (starting), `-` (running, no healthcheck), `stopped`. Only `bad` counts as an alert.
- **Alert flapping:** a container that goes unhealthy, recovers, then goes unhealthy again is a new alert and wakes the screen again, even within the hour.

## Left to the builder

- Fonts, exact text layout, truncation and ordering of containers on the Containers page.
- Polling interval for container health and system stats; debounce and long-press timing.
- GPIO library (libgpiod bindings are what Argon's own tooling uses; prefer whatever installs cleanly in the `python:3.12-slim` image on arm64) and how to pick `/dev/gpiochipN`.
- How to obtain the Pi's LAN IP, hostname and uptime from inside the container (host networking, reading host files via the existing `/host` mount, or another approach).
- Structure: a testable state machine (off / awake / alert) driven by an injected clock and button events, separate from rendering and from hardware.
- Names of new env vars (at minimum: button GPIO line, Docker proxy URL; optionally wake timeout).

## Testing expectations

- Page rendering and the state machine are tested without hardware: dummy luma device, fake clock, fake button events, fake Docker responses.
- Cover: dark by default, wake on press, paging and wrap, timeout to dark, alert wakes by itself, alert stays lit past 30 s, acknowledge, re-wake rules, proxy unreachable, `/health` still healthy while the screen is dark.
- CI continues to run `pytest` and the Docker build. Update `docker-compose.yml`, README and `.dockerignore` as needed. Things only checkable on the Pi (real button line, real panel appearance) are called out in the README.

## Success

Walking into the utility room: the screen is dark; one press shows page 1, which says at a glance whether things are fine; further presses step through the pages; it goes dark by itself. If a container goes unhealthy while nobody is there, the screen is lit and showing the container name when they arrive, until they press the button.
