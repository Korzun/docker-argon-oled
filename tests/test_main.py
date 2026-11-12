from unittest.mock import MagicMock

import pytest
from luma.core.device import dummy

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
