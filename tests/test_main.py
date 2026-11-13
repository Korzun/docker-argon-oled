import json
import queue
import signal
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from luma.core.device import dummy
from luma.core.error import DeviceNotFoundError, DevicePermissionError

import main
from containers import Container
from pages import Snapshot
from state import ButtonEvent, Mode
from sysinfo import HostInfo

STATS = {"cpu": 12.34, "temp": 45.678, "mem": 34.5, "disk": 6.0}
HOST = HostInfo(hostname="pi", ip="192.168.1.42", uptime_s=3600)
GOOD = [Container("coolify", True, "healthy")]
BAD = [Container("coolify", True, "healthy"), Container("postgres", True, "unhealthy")]
SNAP = Snapshot(stats=STATS, containers=GOOD, host=HOST)


@pytest.fixture(autouse=True)
def no_real_button(monkeypatch):
    """Never touch GPIO in tests; individual tests override this."""
    monkeypatch.setattr(main, "start_button", lambda line, chip, out: None)


def test_cpu_temp_converts_millidegrees(tmp_path):
    f = tmp_path / "temp"
    f.write_text("45678\n")
    assert main.cpu_temp(str(f)) == pytest.approx(45.678)


def test_cpu_temp_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        main.cpu_temp(str(tmp_path / "nope"))


def test_read_stats_collects_values(tmp_path, monkeypatch):
    f = tmp_path / "temp"
    f.write_text("50000")
    monkeypatch.setattr(main.psutil, "cpu_percent", lambda: 10.0)
    monkeypatch.setattr(main.psutil, "virtual_memory", lambda: MagicMock(percent=20.0))
    disk_paths = []

    def fake_disk(path):
        disk_paths.append(path)
        return MagicMock(percent=30.0)

    monkeypatch.setattr(main.psutil, "disk_usage", fake_disk)

    stats = main.read_stats(thermal_path=str(f), host_root="/somewhere")

    assert stats == {"cpu": 10.0, "temp": 50.0, "mem": 20.0, "disk": 30.0}
    assert disk_paths == ["/somewhere"]


def test_read_stats_missing_thermal_file_gives_none(tmp_path, monkeypatch):
    monkeypatch.setattr(main.psutil, "cpu_percent", lambda: 1.0)
    monkeypatch.setattr(main.psutil, "virtual_memory", lambda: MagicMock(percent=2.0))
    monkeypatch.setattr(main.psutil, "disk_usage", lambda p: MagicMock(percent=3.0))
    stats = main.read_stats(thermal_path=str(tmp_path / "nope"))
    assert stats["temp"] is None


def test_cli_returns_zero_on_clean_exit(monkeypatch):
    monkeypatch.setattr(main, "main", lambda **k: None)
    assert main.cli(serve_health=False) == 0


def test_cli_explains_missing_display(monkeypatch, capsys):
    def boom():
        raise DeviceNotFoundError("I2C device not found: /dev/i2c-1")

    monkeypatch.setattr(main, "make_device", boom)
    assert main.cli(serve_health=False) == 1
    err = capsys.readouterr().err
    assert "could not talk to the OLED display" in err
    assert "I2C bus 1, address 0x3c" in err
    assert "I2C device not found: /dev/i2c-1" in err
    assert "raspi-config" in err


def test_cli_explains_permission_error(monkeypatch, capsys):
    def boom():
        raise DevicePermissionError("I2C device permission denied: /dev/i2c-1")

    monkeypatch.setattr(main, "make_device", boom)
    assert main.cli(serve_health=False) == 1
    err = capsys.readouterr().err
    assert "permission denied" in err


def test_cli_explains_display_lost_mid_run(monkeypatch, capsys):
    device = dummy(width=main.WIDTH, height=main.HEIGHT)
    monkeypatch.setattr(main, "make_device", lambda: device)
    monkeypatch.setattr(main, "collect_snapshot", lambda url: SNAP)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "start_button", lambda line, chip, out: out.put(ButtonEvent.SHORT))

    def unplug(*a, **k):
        raise DeviceNotFoundError("display disconnected")

    monkeypatch.setattr(device, "display", unplug)
    assert main.cli(serve_health=False) == 1
    assert "display disconnected" in capsys.readouterr().err


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_health_status_starting_ok_and_stale():
    clock = FakeClock()
    hb = main.Heartbeat(clock=clock)
    assert main.health_status(hb, max_age=10)[0] == 503
    assert main.health_status(hb, max_age=10)[1]["status"] == "starting"

    hb.beat()
    clock.now += 4
    code, body = main.health_status(hb, max_age=10)
    assert (code, body["status"]) == (200, "ok")

    clock.now += 7  # 11s since last beat
    code, body = main.health_status(hb, max_age=10)
    assert (code, body["status"]) == (503, "stale")


@pytest.fixture
def health_url():
    clock = FakeClock()
    hb = main.Heartbeat(clock=clock)
    server = main.make_health_server(hb, port=0, host="127.0.0.1", max_age=10)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hb, clock
    server.shutdown()
    server.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_health_endpoint_over_http(health_url):
    base, hb, clock = health_url
    assert _get(base + "/health")[0] == 503  # nothing drawn yet

    hb.beat()
    code, body = _get(base + "/health")
    assert (code, body["status"]) == (200, "ok")

    clock.now += 60
    assert _get(base + "/health")[0] == 503  # loop has gone quiet


def test_health_endpoint_unknown_path_is_404(health_url):
    base, _, _ = health_url
    assert _get(base + "/nope")[0] == 404


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("true", True), ("TRUE", True), ("1", True),
     ("yes", True), ("on", True), ("false", False), ("0", False), ("no", False)],
)
def test_env_flag_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("X_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_FLAG", value)
    assert main.env_flag("X_FLAG") is expected


def test_env_flag_rejects_garbage(monkeypatch):
    monkeypatch.setenv("X_FLAG", "maybe")
    with pytest.raises(ValueError, match="X_FLAG"):
        main.env_flag("X_FLAG")


@pytest.mark.parametrize("env,arg,rotate", [
    (None, None, 0), ("true", None, 2), ("false", None, 0),
    ("false", True, 2), ("true", False, 0),
])
def test_make_device_rotation(monkeypatch, env, arg, rotate):
    import luma.core.interface.serial as serial
    import luma.oled.device as oled

    if env is None:
        monkeypatch.delenv("FLIP_DISPLAY", raising=False)
    else:
        monkeypatch.setenv("FLIP_DISPLAY", env)
    fake_ssd1306 = MagicMock()
    monkeypatch.setattr(serial, "i2c", MagicMock())
    monkeypatch.setattr(oled, "ssd1306", fake_ssd1306)

    main.make_device(flip=arg)

    assert fake_ssd1306.call_args.kwargs["rotate"] == rotate


# --- main loop ------------------------------------------------------------


class RecordingDevice(dummy):
    def __init__(self):
        super().__init__(width=main.WIDTH, height=main.HEIGHT)
        self.calls = []
        self.frames = 0

    def show(self):
        self.calls.append("show")
        super().show()

    def hide(self):
        self.calls.append("hide")
        super().hide()

    def contrast(self, level):
        self.calls.append(("contrast", level))
        super().contrast(level)

    def display(self, image):
        self.frames += 1
        super().display(image)


class Loop:
    """Runs main.main with a fake clock whose sleep advances time by one tick."""

    def __init__(self, containers=GOOD):
        self.clock = FakeClock()
        self.device = RecordingDevice()
        self.events = queue.Queue()
        self.heartbeat = main.Heartbeat(clock=self.clock)
        self.containers = containers
        self.controller = main.Controller(clock=self.clock)

    def collect(self, url):
        return Snapshot(stats=STATS, containers=self.containers, host=HOST)

    def run(self, iterations):
        main.main(
            device=self.device,
            settings=main.Settings(),
            controller=self.controller,
            events=self.events,
            heartbeat=self.heartbeat,
            collect=self.collect,
            clock=self.clock,
            sleep=lambda s: setattr(self.clock, "now", self.clock.now + s),
            iterations=iterations,
        )


def test_dark_by_default_never_draws_but_beats_heartbeat():
    loop = Loop()
    loop.run(50)
    assert loop.device.frames == 0
    assert loop.device.calls == ["hide"]  # once, not every tick
    assert loop.heartbeat.age() is not None


def test_press_wakes_and_draws_overview():
    loop = Loop()
    loop.events.put(ButtonEvent.SHORT)
    loop.run(3)
    assert loop.controller.mode == Mode.AWAKE
    assert "show" in loop.device.calls and ("contrast", 255) in loop.device.calls
    assert loop.device.frames >= 1
    assert loop.device.image.convert("L").getbbox() is not None


def test_press_advances_page_and_changes_drawn_content():
    loop = Loop()
    loop.events.put(ButtonEvent.SHORT)
    loop.run(2)
    first = loop.device.image.tobytes()
    loop.events.put(ButtonEvent.SHORT)
    loop.run(2)
    assert loop.controller.page == 1
    assert loop.device.image.tobytes() != first


def test_unhealthy_container_wakes_without_button_and_uses_low_contrast():
    loop = Loop(containers=BAD)
    loop.run(3)
    assert loop.controller.mode == Mode.ALERT
    assert ("contrast", 64) in loop.device.calls
    assert loop.device.frames >= 1


def test_alert_stays_lit_until_press_then_normal_contrast():
    loop = Loop(containers=BAD)
    loop.run(400)  # 40 simulated seconds, well past the 30 s wake timeout
    assert loop.controller.mode == Mode.ALERT
    loop.events.put(ButtonEvent.SHORT)
    loop.run(3)
    assert loop.controller.mode == Mode.AWAKE
    assert [c for c in loop.device.calls if isinstance(c, tuple)][-1] == ("contrast", 255)


def test_screen_turns_off_after_timeout():
    loop = Loop()
    loop.events.put(ButtonEvent.SHORT)
    loop.run(350)  # 35 simulated seconds
    assert loop.controller.mode == Mode.OFF
    assert loop.device.calls[-1] == "hide"


def test_alert_drifts_content_over_time():
    loop = Loop(containers=BAD)
    loop.run(3)
    before = loop.device.image.tobytes()
    loop.run(620)  # just over 60 simulated seconds
    assert loop.device.image.tobytes() != before


def test_health_stays_ok_while_screen_is_dark():
    loop = Loop()
    loop.run(100)
    assert main.health_status(loop.heartbeat, max_age=1.0)[0] == 200


# --- settings -------------------------------------------------------------


def test_settings_defaults_and_overrides():
    d = main.Settings.from_env({})
    assert (d.proxy_url, d.button_line, d.button_chip, d.health_port) == (
        "http://127.0.0.1:2375", 4, None, 8099
    )
    o = main.Settings.from_env(
        {"DOCKER_PROXY_URL": "http://h:1", "BUTTON_GPIO": "17", "BUTTON_CHIP": "/dev/gpiochip4", "HEALTH_PORT": "9000"}
    )
    assert (o.proxy_url, o.button_line, o.button_chip, o.health_port) == ("http://h:1", 17, "/dev/gpiochip4", 9000)


def test_settings_rejects_non_numeric_gpio():
    with pytest.raises(ValueError, match="BUTTON_GPIO"):
        main.Settings.from_env({"BUTTON_GPIO": "four"})


def test_health_server_defaults_to_loopback():
    server = main.make_health_server(main.Heartbeat(), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


# --- cli ------------------------------------------------------------------


def test_cli_explains_button_unavailable(monkeypatch, capsys):
    def boom(line, chip, out):
        raise main.ButtonUnavailableError("GPIO device /dev/gpiochip0 does not exist")

    monkeypatch.setattr(main, "start_button", boom)
    assert main.cli(serve_health=False) == 1
    err = capsys.readouterr().err
    assert "button" in err.lower()
    assert "GPIO line 4" in err
    assert "/dev/gpiochip0 does not exist" in err
    assert "Argon" in err


def test_cli_explains_bad_settings(monkeypatch, capsys):
    monkeypatch.setenv("BUTTON_GPIO", "four")
    assert main.cli(serve_health=False) == 1
    assert "BUTTON_GPIO" in capsys.readouterr().err


# --- review fixes ---------------------------------------------------------


def test_collect_failure_does_not_crash_loop_and_keeps_last_snapshot(capsys):
    loop = Loop()
    loop.events.put(ButtonEvent.SHORT)
    calls = {"n": 0, "frames": None}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 3:
            calls["frames"] = loop.device.frames
            loop.events.put(ButtonEvent.SHORT)  # a press arrives while collection is failing
        if calls["n"] >= 3:
            raise OSError("disk went away")
        return Snapshot(stats=STATS, containers=GOOD, host=HOST)

    loop.collect = flaky
    loop.run(100)  # 10 simulated seconds: several failing refreshes
    assert calls["n"] > 3
    assert loop.controller.page == 1
    assert loop.device.frames > calls["frames"]  # the press still drew, from the last good snapshot
    err = capsys.readouterr().err
    assert err.count("could not collect status") == 1  # logged once per failure streak
    assert "disk went away" in err


def test_collect_failing_from_the_start_still_beats_heartbeat_and_does_not_crash():
    loop = Loop()

    def boom(url):
        raise OSError("no /host")

    loop.collect = boom
    loop.events.put(ButtonEvent.SHORT)
    loop.run(30)
    assert loop.heartbeat.age() is not None
    assert loop.device.frames == 0  # nothing to draw yet, but no crash


def test_alert_page_keeps_names_when_the_proxy_drops(monkeypatch):
    seen = []
    real = main.build_page

    def spy(page, snap, alert=False):
        seen.append((alert, snap.containers))
        return real(page, snap, alert=alert)

    monkeypatch.setattr(main, "build_page", spy)
    loop = Loop()
    calls = {"n": 0}

    def proxy_drops(url):
        calls["n"] += 1
        containers = BAD if calls["n"] <= 2 else None  # proxy restarts during maintenance
        return Snapshot(stats=STATS, containers=containers, host=HOST)

    loop.collect = proxy_drops
    loop.run(100)
    assert calls["n"] > 3
    assert loop.controller.mode == Mode.ALERT
    alert, shown = seen[-1]
    assert alert is True
    assert shown is not None and any(c.name == "postgres" and c.health == "unhealthy" for c in shown)


def test_button_failure_on_the_queue_stops_the_loop_with_the_error():
    loop = Loop()
    loop.events.put(main.ButtonUnavailableError("button input stopped: gone"))
    with pytest.raises(main.ButtonUnavailableError, match="gone"):
        loop.run(3)


@pytest.fixture
def restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_cli_installs_sigterm_handler_that_exits_cleanly(monkeypatch, restore_sigterm):
    monkeypatch.setattr(main, "main", lambda **k: None)
    main.cli(serve_health=False)
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    with pytest.raises(SystemExit) as exc:
        handler(signal.SIGTERM, None)  # SystemExit lets luma's atexit hook switch the panel off
    assert exc.value.code == 0


def test_every_documented_env_var_is_forwarded_by_compose():
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md")).read()
    compose = open(os.path.join(root, "docker-compose.yml")).read()
    table = readme.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    names = re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", table, re.M)
    assert names, "no variables found in the README Configuration table"
    missing = [n for n in names if n not in compose]
    assert not missing, f"documented but not forwarded by docker-compose.yml: {missing}"


# --- deferred minors ------------------------------------------------------


def test_cli_explains_health_port_clash(monkeypatch, capsys, restore_sigterm):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = blocker.getsockname()[1]
        assert main.cli(settings=main.Settings(health_port=port)) == 1
    finally:
        blocker.close()
    err = capsys.readouterr().err
    assert "/health" in err and "HEALTH_PORT" in err and str(port) in err


def test_settings_strip_trailing_slash_from_proxy_url():
    assert main.Settings.from_env({"DOCKER_PROXY_URL": "http://h:1/"}).proxy_url == "http://h:1"


def test_proxy_failure_and_recovery_are_logged_once_each(capsys):
    loop = Loop()
    calls = {"n": 0}

    def flaky_proxy(url):
        calls["n"] += 1
        down = 2 <= calls["n"] <= 4
        return Snapshot(stats=STATS, containers=None if down else GOOD, host=HOST)

    loop.collect = flaky_proxy
    loop.run(100)
    err = capsys.readouterr().err
    assert err.count("Docker proxy unreachable") == 1
    assert err.count("Docker proxy reachable again") == 1
    assert "http://127.0.0.1:2375" in err


def test_alert_title_bar_is_not_inverted_but_normal_pages_are():
    def title_fraction(device):
        band = device.image.convert("L").crop((0, 0, main.WIDTH, 10))
        return sum(1 for p in band.getdata() if p) / (main.WIDTH * 10)

    calm = Loop()
    calm.events.put(ButtonEvent.SHORT)
    calm.run(3)
    alert = Loop(containers=BAD)
    alert.run(3)
    assert title_fraction(calm.device) > 0.5
    assert title_fraction(alert.device) < 0.25


def test_proxy_port_is_configurable_in_compose():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose = open(os.path.join(root, "docker-compose.yml")).read()
    assert "127.0.0.1:${PROXY_PORT:-2375}:2375" in compose
    assert "DOCKER_PROXY_URL=http://127.0.0.1:${PROXY_PORT:-2375}" in compose


def test_every_compose_service_has_a_healthcheck():
    """Coolify reports a stack as having no healthcheck if any service lacks one."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose = open(os.path.join(root, "docker-compose.yml")).read()
    body = compose.split("services:\n", 1)[1]
    blocks = re.split(r"^  (?=[a-z][\w-]*:\s*$)", body, flags=re.M)[1:]
    assert len(blocks) >= 2, "expected the oled and docker-proxy services"
    missing = [b.split(":", 1)[0] for b in blocks if "healthcheck:" not in b]
    assert not missing, f"services without a healthcheck: {missing}"
