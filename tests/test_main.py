from unittest.mock import MagicMock

import pytest
from luma.core.device import dummy
from luma.core.error import DeviceNotFoundError, DevicePermissionError

import main

STATS = {"cpu": 12.34, "temp": 45.678, "mem": 34.5, "disk": 6.0}


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


def test_render_text_and_positions():
    draw = MagicMock()
    main.render(draw, STATS)
    calls = [(c.args[0], c.args[1]) for c in draw.text.call_args_list]
    assert calls == [
        ((0, 0), "CPU   12.3%"),
        ((0, 14), "Temp  45.7C"),
        ((0, 28), "Mem   34.5%"),
        ((0, 42), "Disk   6.0%"),
    ]


def test_render_draws_pixels_in_each_row_on_dummy_device():
    device = dummy(width=main.WIDTH, height=main.HEIGHT)
    with main.canvas(device) as draw:
        main.render(draw, STATS)
    image = device.image.convert("L")
    for top in (0, 14, 28, 42):
        band = image.crop((0, top, main.WIDTH, top + 12))
        assert band.getbbox() is not None, f"nothing drawn at y={top}"


def test_main_runs_requested_iterations(monkeypatch):
    monkeypatch.setattr(main, "read_stats", lambda: STATS)
    device = dummy(width=main.WIDTH, height=main.HEIGHT)
    main.main(device=device, interval=0, iterations=3)
    assert device.image.convert("L").getbbox() is not None


def test_read_stats_missing_thermal_file_gives_none(tmp_path, monkeypatch):
    monkeypatch.setattr(main.psutil, "cpu_percent", lambda: 1.0)
    monkeypatch.setattr(main.psutil, "virtual_memory", lambda: MagicMock(percent=2.0))
    monkeypatch.setattr(main.psutil, "disk_usage", lambda p: MagicMock(percent=3.0))
    stats = main.read_stats(thermal_path=str(tmp_path / "nope"))
    assert stats["temp"] is None


def test_render_shows_na_when_temp_missing():
    draw = MagicMock()
    main.render(draw, {**STATS, "temp": None})
    texts = [c.args[1] for c in draw.text.call_args_list]
    assert "Temp   n/a" in texts


def test_cli_returns_zero_on_clean_exit(monkeypatch):
    monkeypatch.setattr(main, "main", lambda: None)
    assert main.cli() == 0


def test_cli_explains_missing_display(monkeypatch, capsys):
    def boom():
        raise DeviceNotFoundError("I2C device not found: /dev/i2c-1")

    monkeypatch.setattr(main, "make_device", boom)
    assert main.cli() == 1
    err = capsys.readouterr().err
    assert "could not talk to the OLED display" in err
    assert "I2C bus 1, address 0x3c" in err
    assert "I2C device not found: /dev/i2c-1" in err
    assert "raspi-config" in err


def test_cli_explains_permission_error(monkeypatch, capsys):
    def boom():
        raise DevicePermissionError("I2C device permission denied: /dev/i2c-1")

    monkeypatch.setattr(main, "make_device", boom)
    assert main.cli() == 1
    err = capsys.readouterr().err
    assert "permission denied" in err


def test_cli_explains_display_lost_mid_run(monkeypatch, capsys):
    device = dummy(width=main.WIDTH, height=main.HEIGHT)
    monkeypatch.setattr(main, "make_device", lambda: device)
    monkeypatch.setattr(main, "read_stats", lambda: STATS)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    def unplug(*a, **k):
        raise DeviceNotFoundError("display disconnected")

    monkeypatch.setattr(device, "display", unplug)
    assert main.cli() == 1
    assert "display disconnected" in capsys.readouterr().err
