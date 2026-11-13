import socket

import pytest

import sysinfo


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (45, "0m"),
        (300, "5m"),
        (3 * 3600 + 12 * 60, "3h 12m"),
        (12 * 86400 + 3 * 3600, "12d 3h"),
    ],
)
def test_format_uptime(seconds, expected):
    assert sysinfo.format_uptime(seconds) == expected


def test_read_host_info_reads_hostname_file(tmp_path):
    f = tmp_path / "hostname"
    f.write_text("pi-basement\n")
    info = sysinfo.read_host_info(hostname_path=str(f), ip_override="10.0.0.5")
    assert info.hostname == "pi-basement"


def test_read_host_info_falls_back_to_socket_hostname(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "fallback-host")
    info = sysinfo.read_host_info(hostname_path=str(tmp_path / "missing"), ip_override="10.0.0.5")
    assert info.hostname == "fallback-host"


def test_ip_override_wins(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("local_ip must not be called when an override is given")

    monkeypatch.setattr(sysinfo, "local_ip", boom)
    info = sysinfo.read_host_info(hostname_path=str(tmp_path / "x"), ip_override="10.0.0.5")
    assert info.ip == "10.0.0.5"


def test_local_ip_none_when_no_route(monkeypatch):
    def no_socket(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(socket, "socket", no_socket)
    assert sysinfo.local_ip() is None


def test_read_host_info_uptime_is_positive(tmp_path):
    info = sysinfo.read_host_info(hostname_path=str(tmp_path / "x"), ip_override="1.2.3.4")
    assert info.uptime_s > 0
