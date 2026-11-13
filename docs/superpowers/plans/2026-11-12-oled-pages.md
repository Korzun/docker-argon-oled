# OLED Pages, Button and Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the always-on stats screen into a dark-by-default, button-paged status board that wakes itself and stays lit when a container is unhealthy.

**Architecture:** Split `main.py` into small flat modules with one job each: `sysinfo` (host facts), `containers` (read-only Docker proxy client), `state` (pure off/awake/alert state machine with an injected clock), `pages` (content + drawing), `button` (GPIO edge source + press classifier). `main.py` only wires them in a 0.1 s tick loop that always beats the heartbeat, whether or not the screen is lit. Everything except the real GPIO line and the real panel is testable without hardware.

**Tech Stack:** Python 3.12, luma.oled/luma.core (SSD1306), psutil, libgpiod bindings (lazy-imported), pytest, Docker Compose, Tecnativa-style docker-socket-proxy.

**Spec:** `docs/superpowers/specs/2026-11-12-oled-pages-design.md`

## Global Constraints

- Display: SSD1306 128x64, I2C bus 1, address 0x3C, via luma. Off = `device.hide()`; on = `device.show()`. `FLIP_DISPLAY` keeps working unchanged.
- Wake timeout 30 s; acknowledged-but-persistent problem re-wakes after 3600 s; long press at 1.0 s; alert drift 1-2 px about every 60 s; alert uses reduced contrast.
- Pages in order: Overview, Containers, Network, System (`n/4` in an inverted title bar, plain rows below, monospace font). Pages never auto-cycle.
- Alert = Docker health status `unhealthy` only. Docker access is read-only through a socket-proxy container (`CONTAINERS=1` only, writes disabled, socket mounted read-only into the proxy only). The app never mounts the Docker socket.
- Button: default GPIO line 4, active-low with internal pull-up, configurable. Short press acts on release; long press fires once while held.
- `oled` service uses `network_mode: host`; proxy published on `127.0.0.1` only; `/health` binds `127.0.0.1` on configurable `HEALTH_PORT` (default 8099, not 8080).
- `/health` must stay healthy while the screen is dark.
- Image is `python:3.12-slim` on arm64 (Pi 5); CI builds on x86 only, so GPIO imports stay lazy and tests never need `gpiod`.
- Do not use, copy from, commit, or ship Argon's scripts. Stage files by name, never `git add -A`. Delete untracked `argon1v5-2.sh` in Task 7.

## Review Focus

- Docker proxy returns an error status, times out, or returns malformed JSON: pages show `n/a`, no alert, no crash. (Task 2, Task 6)
- Proxy becomes unreachable while an alert is lit: unknown is not "recovered"; the screen stays lit. (Task 3)
- Very long or non-ASCII container names, and 50+ containers: truncated to the screen, never overflowing, and bad containers listed first. (Task 4)
- Button noise: duplicate press edges, a release with no prior press, press held through the wake timeout, bounce shorter than debounce. (Task 5)
- GPIO unavailable or line busy at startup (for example Argon's own daemon holding the line): clear explanation and exit 1, not a traceback. (Task 5, Task 6)

---

### Task 1: Host facts

**Files:**
- Create: `sysinfo.py`
- Test: `tests/test_sysinfo.py`

**Interfaces:**
- Produces: `HostInfo` (frozen dataclass: `hostname: str`, `ip: str | None`, `uptime_s: float`); `format_uptime(seconds: float) -> str`; `local_ip() -> str | None`; `read_host_info(hostname_path: str = "/host/etc/hostname", ip_override: str | None = None) -> HostInfo`.

- [ ] **Step 1: Write failing tests.** `test_format_uptime` asserts `format_uptime(45)=="0m"`, `(300)=="5m"`, `(3*3600+12*60)=="3h 12m"`, `(12*86400+3*3600)=="12d 3h"`. `test_read_host_info_reads_hostname_file` (tmp file `"pi-basement\n"` -> `hostname=="pi-basement"`). `test_read_host_info_falls_back_to_socket_hostname` (missing file, monkeypatch `socket.gethostname`). `test_ip_override_wins` (`ip_override="10.0.0.5"` -> `ip=="10.0.0.5"` without calling `local_ip`). `test_local_ip_none_when_no_route` (monkeypatch `socket.socket` to raise `OSError` -> `None`).
- [ ] **Step 2: Run** `pytest tests/test_sysinfo.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the four names above. `local_ip` uses the UDP-connect trick to a TEST-NET address (no packet is sent) and returns `getsockname()[0]`; `uptime_s = time.time() - psutil.boot_time()`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add sysinfo.py tests/test_sysinfo.py && git commit -m "Add host info helpers"`.

### Task 2: Docker proxy client

**Files:**
- Create: `containers.py`
- Test: `tests/test_containers.py`

**Interfaces:**
- Produces: `Container` (frozen dataclass: `name: str`, `running: bool`, `health: str | None` where health is `"healthy" | "unhealthy" | "starting" | None`); `parse_containers(payload: list[dict]) -> list[Container]`; `status_label(c: Container) -> str` returning `"ok"`, `"bad"`, `".."`, `"-"` (running, no healthcheck), or `"stopped"`; `unhealthy_names(containers: list[Container] | None) -> frozenset[str] | None` (None in, None out); `fetch_containers(base_url: str, opener=urllib.request.urlopen, timeout: float = 2.0) -> list[Container] | None` (None on any network, HTTP, or JSON failure).

- [ ] **Step 1: Write failing tests** using a fixture shaped like Docker's `GET /containers/json?all=1`: `Names: ["/postgres"]`, `State: "running"`, `Status: "Up 3 hours (unhealthy)"`. Cases: `(healthy)` -> `"healthy"`; `(health: starting)` -> `"starting"`; plain `Up 2 days` -> `None` and label `"-"`; `State: "exited"` -> `running False`, label `"stopped"`. `test_unhealthy_names_returns_only_bad`; `test_unhealthy_names_none_passthrough`. `test_fetch_returns_none_on_urlerror`, `..._on_http_500`, `..._on_malformed_json` (fake `opener`). `test_fetch_requests_all_containers` asserts the requested URL is `<base>/containers/json?all=1`.
- [ ] **Step 2: Run** `pytest tests/test_containers.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Health comes from the `Status` text; strip the leading `/` from names. **Verify the field format against the real proxy in Task 7** (`curl http://127.0.0.1:2375/containers/json?all=1`); if the real output differs, fix the parser and this task's fixture.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add containers.py tests/test_containers.py && git commit -m "Add read-only Docker proxy client"`.

### Task 3: State machine

**Files:**
- Create: `state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `ButtonEvent` (Enum: `SHORT`, `LONG`); `Mode` (Enum: `OFF`, `AWAKE`, `ALERT`); constants `PAGE_OVERVIEW, PAGE_CONTAINERS, PAGE_NETWORK, PAGE_SYSTEM = 0, 1, 2, 3` and `PAGE_COUNT = 4`; `Controller(clock=time.monotonic, wake_timeout=30.0, rewake_after=3600.0)` with read-only properties `mode: Mode`, `page: int` and methods `on_button(event: ButtonEvent) -> None`, `on_containers(unhealthy: frozenset[str] | None) -> None` (None = unknown), `tick() -> None`, `alert_elapsed() -> float` (0.0 outside ALERT).

Behaviour, all pinned by tests: starts `OFF` on page 0. From `OFF`, any press -> `AWAKE`, page 0 (a first press only wakes). In `AWAKE`, `SHORT` -> next page, wrapping 3 -> 0; `LONG` -> page 0; each press restarts the 30 s timeout; `tick()` at/after the deadline -> `OFF`. A name in `unhealthy` that is not acknowledged -> `ALERT` on `PAGE_CONTAINERS` (from OFF or AWAKE). In `ALERT`, any press acknowledges all current names -> `AWAKE` on `PAGE_CONTAINERS` with a fresh 30 s timeout. If the unhealthy set becomes empty while in `ALERT`, -> `OFF`. `None` never changes mode. Acknowledgement is forgotten for names that leave the set, so a flapping container is a new alert. If acknowledged names are still unhealthy `rewake_after` seconds after acknowledgement, -> `ALERT` again (repeats hourly).

- [ ] **Step 1: Write failing tests** with a `FakeClock` (callable with a mutable `now`): `test_starts_dark`, `test_first_press_wakes_to_overview_without_advancing`, `test_short_press_advances_and_wraps`, `test_long_press_returns_to_overview`, `test_timeout_turns_off_after_30s_but_not_before` (29.9 s awake, 30.0 s off), `test_press_restarts_timeout`, `test_unhealthy_wakes_from_dark_on_containers_page`, `test_alert_persists_past_30s`, `test_press_acknowledges_alert_then_times_out_normally`, `test_new_unhealthy_name_alerts_while_awake`, `test_flapping_container_realerts_immediately`, `test_alert_clears_to_off_when_all_recover`, `test_unknown_snapshot_keeps_alert_lit`, `test_unknown_snapshot_does_not_wake`, `test_acknowledged_problem_rewakes_after_one_hour` (3599 s no, 3600 s yes), `test_alert_elapsed_counts_from_alert_start`.
- [ ] **Step 2: Run** `pytest tests/test_state.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `Controller` as pure logic; it holds the set of currently unhealthy names, the acknowledged subset, deadlines and the clock.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add state.py tests/test_state.py && git commit -m "Add display state machine"`.

### Task 4: Pages

**Files:**
- Create: `pages.py`
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: `Container`, `status_label` (Task 2); `HostInfo`, `format_uptime` (Task 1); `PAGE_*` (Task 3).
- Produces: `Snapshot` (frozen dataclass: `stats: dict` with keys `cpu, temp, mem, disk` as `read_stats` returns, `containers: list[Container] | None`, `host: HostInfo`); `PageContent` (frozen dataclass: `title: str`, `counter: str`, `rows: list[str]`); `build_page(page: int, snap: Snapshot, alert: bool = False) -> PageContent`; `drift_offset(elapsed_s: float) -> tuple[int, int]`; `load_font() -> ImageFont` (DejaVu Sans Mono from `/usr/share/fonts/truetype/dejavu/`, falls back to Pillow's default when absent); `draw_page(draw, content: PageContent, offset: tuple[int, int] = (0, 0)) -> None` (inverted title bar with the counter right-aligned, rows below).

Content rules: Overview rows `Temp    45.6C` (`n/a` when None), `Uptime  12d 3h`, `Ctrs    7 up, 0 bad` (`n/a` when containers is None), `IP      192.168.1.42` (`n/a` when None); title `ALL OK`, or `! N BAD` when any bad, or `OVERVIEW` when containers unknown. Containers: title `CONTAINERS`, rows `<name> <status_label>` sorted bad, starting, running, stopped, then name; at most 4 rows, the last becoming `+N more` when truncated; `n/a` row when unknown. Network: hostname and IP rows. System: `CPU   12.3%`, `Mem   34.5%`, `Disk  56.7%`, `Temp  45.6C`. With `alert=True` the page is the alert page: title `! UNHEALTHY`, counter `2/4`, up to 3 bad container names then `+N more`, final row `press = ack`. Names longer than the row budget are truncated with no wrap. `drift_offset` cycles `(0,0),(1,0),(1,1),(0,1)` advancing every 60 s.

- [ ] **Step 1: Write failing tests** on `build_page` with exact strings: `test_overview_all_ok`, `test_overview_with_bad_containers_title`, `test_overview_docker_unreachable_shows_na`, `test_containers_sorted_bad_first_and_truncated_with_more_row` (50 containers), `test_containers_long_and_non_ascii_names_truncated` (no row exceeds 21 characters), `test_network_page`, `test_system_page`, `test_alert_page_lists_only_bad_names`, `test_drift_offset_cycles_every_minute`. On a luma `dummy(128, 64)` device: `test_draw_page_inverts_title_bar` (title band mostly lit, row bands have lit pixels), `test_draw_page_offset_shifts_pixels_by_one`, `test_draw_page_stays_inside_screen_for_every_page_and_offset` (nothing drawn out of bounds).
- [ ] **Step 2: Run** `pytest tests/test_pages.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the names above. Fit font size so 21 columns fit in 128 px; check on the real panel in Task 7.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add pages.py tests/test_pages.py && git commit -m "Add page content and drawing"`.

### Task 5: Button input

**Files:**
- Create: `button.py`
- Test: `tests/test_button.py`

**Interfaces:**
- Consumes: `ButtonEvent` (Task 3).
- Produces: `ButtonUnavailableError(Exception)`; `PressClassifier(long_press_s: float = 1.0, debounce_s: float = 0.03, clock=time.monotonic)` with `on_edge(pressed: bool) -> ButtonEvent | None` and `poll() -> ButtonEvent | None`; `EdgeSource` protocol with `wait_edge(timeout: float) -> bool | None` (True pressed, False released, None on timeout); `pump_events(source, classifier, out: queue.Queue, stop: threading.Event) -> None`; `GpiodEdgeSource(line: int, chip: str | None = None)` (falling edge = pressed; `chip=None` tries `/dev/gpiochip4` then `/dev/gpiochip0`; raises `ButtonUnavailableError` with a plain-language reason on missing device, permission denied or line busy); `start_button(line: int, chip: str | None, out: queue.Queue) -> threading.Thread` (daemon).

Classifier rules: release after a hold shorter than `long_press_s` and at least `debounce_s` -> `SHORT`; hold reaching `long_press_s` -> `LONG` once from `poll()`, and the later release yields nothing; a hold shorter than debounce, a duplicate press, or a release with no press yields nothing.

- [ ] **Step 1: Write failing tests** with a fake clock: `test_short_press_fires_on_release`, `test_long_press_fires_once_while_held_and_not_on_release`, `test_bounce_shorter_than_debounce_ignored`, `test_duplicate_press_edges_ignored`, `test_release_without_press_ignored`, `test_hold_across_poll_boundaries`. `test_pump_events_puts_classified_events_on_queue` using a scripted fake `EdgeSource` and a stop event. `test_gpiod_source_import_is_lazy` (importing `button` works with no `gpiod` installed). `test_gpiod_source_missing_device_raises_unavailable` (nonexistent chip path).
- [ ] **Step 2: Run** `pytest tests/test_button.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pick the libgpiod binding that installs cleanly in the image (fallback: `gpiozero` with `lgpio`); import it inside `GpiodEdgeSource.__init__` only. Request the line with falling and rising edge detection and an internal pull-up.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add button.py tests/test_button.py && git commit -m "Add button input"`.

### Task 6: Wire it together

**Files:**
- Modify: `main.py` (replace `render` and the old `main`; keep `cpu_temp`, `read_stats`, `env_flag`, `make_device`, `Heartbeat`, `health_status`, `cli`)
- Modify: `tests/test_main.py` (delete `test_render_*` and the old `test_main_*` tests; keep the rest, adjusting `cli` tests to the new signatures)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: `Settings` (frozen dataclass: `proxy_url: str`, `button_line: int`, `button_chip: str | None`, `health_port: int`) with `Settings.from_env(environ=os.environ) -> Settings` reading `DOCKER_PROXY_URL` (default `http://127.0.0.1:2375`), `BUTTON_GPIO` (4), `BUTTON_CHIP` (unset), `HEALTH_PORT` (8099); `collect_snapshot(proxy_url: str) -> Snapshot`; `apply_mode(device, mode: Mode, previous: Mode | None) -> None` (hide on OFF, show on others, contrast 255 for AWAKE and 64 for ALERT, only on transitions); `main(device=None, settings=None, controller=None, events: queue.Queue | None = None, heartbeat=None, collect=collect_snapshot, clock=time.monotonic, sleep=time.sleep, tick: float = 0.1, iterations: int | None = None) -> None`. `make_health_server` default `host` becomes `"127.0.0.1"`; `HEALTH_PORT` module constant becomes 8099.

Loop rules: each tick drains `events` into `controller.on_button`, refreshes the snapshot every `REFRESH_SECONDS` (calling `controller.on_containers(unhealthy_names(snapshot.containers))`), calls `controller.tick()`, applies mode changes, draws only when not `OFF` and the mode, page, or snapshot changed (using `drift_offset(controller.alert_elapsed())` in ALERT), and always beats the heartbeat. `cli` starts the button thread and the health server on `127.0.0.1:settings.health_port`, and prints a plain-language reason and returns 1 on `ButtonUnavailableError`, mentioning `/dev/gpiochip*` passthrough and that another program (such as Argon's own daemon) may be holding the line.

- [ ] **Step 1: Write failing tests** with a `dummy`-based device subclass recording `show`/`hide`/`contrast`/`display` calls, a fake `collect`, a fake clock, and a plain `queue.Queue`: `test_dark_by_default_never_draws_but_beats_heartbeat`, `test_press_wakes_and_draws_overview`, `test_press_advances_page_and_changes_drawn_content`, `test_unhealthy_container_wakes_without_button_and_uses_low_contrast`, `test_alert_stays_lit_until_press_then_normal_contrast`, `test_screen_turns_off_after_timeout`, `test_hide_called_once_per_transition_not_every_tick`, `test_settings_defaults_and_overrides`, `test_settings_rejects_non_numeric_gpio`, `test_cli_explains_button_unavailable` (stderr mentions the line and chip, exit code 1), `test_health_server_defaults_to_loopback`.
- [ ] **Step 2: Run** `pytest -v`. Expected: the new tests FAIL; the retained tests still pass.
- [ ] **Step 3: Implement** the interfaces above, keeping the existing graceful display-missing handling.
- [ ] **Step 4: Run** `pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git add main.py tests/test_main.py && git commit -m "Wire pages, button and alerts into the main loop"`.

### Task 7: Deployment, docs, cleanup, and checks on the Pi

**Files:**
- Modify: `docker-compose.yml`, `dockerfile`, `.github/workflows/ci.yml`, `README.md`, `docs/superpowers/specs/2026-11-12-oled-pages-design.md` (confirmed GPIO values)
- Delete: `argon1v5-2.sh` (untracked, owner-approved)

- [ ] **Step 1: Compose.** Add a `docker-socket-proxy` service (pinned image tag checked for arm64; `CONTAINERS=1`, `POST=0`; `/var/run/docker.sock:/var/run/docker.sock:ro`; `ports: ["127.0.0.1:2375:2375"]`). Change `oled` to `network_mode: host`, `depends_on` the proxy, add `devices: ["${GPIO_DEVICE:-/dev/gpiochip0}:${GPIO_DEVICE:-/dev/gpiochip0}", "/dev/i2c-1:/dev/i2c-1"]` and `environment` entries `FLIP_DISPLAY`, `BUTTON_GPIO=${BUTTON_GPIO:-4}`, `BUTTON_CHIP=${GPIO_DEVICE:-/dev/gpiochip0}`, `DOCKER_PROXY_URL`, `HEALTH_PORT=${HEALTH_PORT:-8099}`. Change the healthcheck command to read `HEALTH_PORT` from the container environment.
- [ ] **Step 2: Dockerfile.** Add the GPIO library (and any apt package it needs) chosen in Task 5.
- [ ] **Step 3: CI.** Add `docker compose config -q` to the `docker` job. Run `pytest -v` and `docker compose config -q` locally. Expected: PASS, no output.
- [ ] **Step 4: README.** Document pages and button behaviour, the proxy and why the socket isn't mounted directly, the new env vars (`GPIO_DEVICE`, `BUTTON_GPIO`, `HEALTH_PORT`, `DOCKER_PROXY_URL`), and a Troubleshooting entry for GPIO errors.
- [ ] **Step 5: Delete** `argon1v5-2.sh` and confirm `git status --short` no longer lists it.
- [ ] **Step 6: Commit** by name: `git add docker-compose.yml dockerfile .github/workflows/ci.yml README.md docs/superpowers/specs/2026-11-12-oled-pages-design.md && git commit -m "Deploy proxy, GPIO and host networking"`. Push only when the owner says so, then check the CI run is green.
- [ ] **Step 7: On the Pi (owner runs, in order).** (a) Run `probe_button.py` and `gpiodetect`; set Coolify env `GPIO_DEVICE` and `BUTTON_GPIO` to the confirmed values and record them in the spec. (b) Deploy. (c) `curl -s 'http://127.0.0.1:2375/containers/json?all=1'` and confirm the `Status` text matches the Task 2 fixture; fix the parser if not. (d) Screen is dark; one press shows Overview; presses step through all four pages and wrap; a long press returns to Overview; the screen goes dark after about 30 s; the title bar and rows fit without clipping. (e) `docker run -d --name bad-test --health-cmd false --health-interval 5s --health-retries 1 alpine sleep 600`: within about 10 s the screen wakes by itself on the Containers page naming `bad-test` and stays lit; a press acknowledges it; `docker rm -f bad-test` and confirm the screen goes dark when the alert clears unacknowledged (repeat the run without pressing). (f) `docker compose ps` shows the oled container healthy while the screen is dark.
