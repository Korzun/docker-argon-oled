from __future__ import annotations

import socket
import time
from dataclasses import dataclass

import psutil

HOSTNAME_PATH = "/host/etc/hostname"
_TEST_NET_ADDR = ("192.0.2.1", 9)  # TEST-NET-1; connecting a UDP socket sends nothing


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    ip: str | None
    uptime_s: float


def format_uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(_TEST_NET_ADDR)
            return s.getsockname()[0]
    except OSError:
        return None


def _hostname(path: str) -> str:
    try:
        with open(path) as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    return socket.gethostname()


def read_host_info(hostname_path: str = HOSTNAME_PATH, ip_override: str | None = None) -> HostInfo:
    ip = ip_override if ip_override else local_ip()
    return HostInfo(
        hostname=_hostname(hostname_path),
        ip=ip,
        uptime_s=time.time() - psutil.boot_time(),
    )
