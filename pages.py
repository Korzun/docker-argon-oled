from __future__ import annotations

import functools
from dataclasses import dataclass

from PIL import ImageFont

from containers import Container, status_label
from state import PAGE_CONTAINERS, PAGE_COUNT, PAGE_NETWORK, PAGE_OVERVIEW, PAGE_SYSTEM
from sysinfo import HostInfo, format_uptime

WIDTH = 128
HEIGHT = 64
COLS = 21
TITLE_H = 11
ROW_H = 11
MAX_ROWS = 4
FONT_SIZE = 9
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_DRIFT = [(0, 0), (1, 0), (1, 1), (0, 1)]
_ORDER = {"bad": 0, "..": 1, "ok": 2, "-": 2, "stopped": 3}


@dataclass(frozen=True)
class Snapshot:
    stats: dict
    containers: list[Container] | None
    host: HostInfo


@dataclass(frozen=True)
class PageContent:
    title: str
    counter: str
    rows: list[str]


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _fit(text: str) -> str:
    return _ascii(text)[:COLS]


def _temp(value) -> str:
    return "n/a" if value is None else f"{value:.1f}C"


def _bad(containers: list[Container]) -> list[str]:
    return sorted(c.name for c in containers if c.health == "unhealthy")


def _overview(snap: Snapshot) -> tuple[str, list[str]]:
    cs = snap.containers
    if cs is None:
        title, ctrs = "OVERVIEW", "n/a"
    else:
        bad = len(_bad(cs))
        title = f"! {bad} BAD" if bad else "ALL OK"
        ctrs = f"{sum(1 for c in cs if c.running)} up, {bad} bad"
    rows = [
        f"Temp    {_temp(snap.stats['temp'])}",
        f"Uptime  {format_uptime(snap.host.uptime_s)}",
        f"Ctrs    {ctrs}",
        f"IP      {snap.host.ip or 'n/a'}",
    ]
    return title, rows


def _container_row(c: Container) -> str:
    status = status_label(c)
    width = COLS - len(status) - 1
    return f"{_ascii(c.name)[:width]:<{width}} {status}"


def _containers(snap: Snapshot) -> list[str]:
    if snap.containers is None:
        return ["Docker n/a"]
    ordered = sorted(snap.containers, key=lambda c: (_ORDER[status_label(c)], c.name.lower()))
    if len(ordered) <= MAX_ROWS:
        return [_container_row(c) for c in ordered]
    shown = [_container_row(c) for c in ordered[: MAX_ROWS - 1]]
    return shown + [f"+{len(ordered) - (MAX_ROWS - 1)} more"]


def _alert_rows(snap: Snapshot) -> list[str]:
    if snap.containers is None:
        return ["Docker n/a", "press = ack"]
    names = _bad(snap.containers)
    if len(names) <= MAX_ROWS - 1:
        shown = [_fit(n) for n in names]
    else:
        shown = [_fit(n) for n in names[: MAX_ROWS - 2]] + [f"+{len(names) - (MAX_ROWS - 2)} more"]
    return shown + ["press = ack"]


def build_page(page: int, snap: Snapshot, alert: bool = False) -> PageContent:
    counter = f"{page + 1}/{PAGE_COUNT}"
    if alert:
        return PageContent("! UNHEALTHY", counter, _alert_rows(snap))
    if page == PAGE_OVERVIEW:
        title, rows = _overview(snap)
    elif page == PAGE_CONTAINERS:
        title, rows = "CONTAINERS", _containers(snap)
    elif page == PAGE_NETWORK:
        title, rows = "NETWORK", [_fit(snap.host.hostname), snap.host.ip or "n/a"]
    elif page == PAGE_SYSTEM:
        s = snap.stats
        temp = "  n/a" if s["temp"] is None else f"{s['temp']:5.1f}C"
        title = "SYSTEM"
        rows = [
            f"CPU  {s['cpu']:5.1f}%",
            f"Mem  {s['mem']:5.1f}%",
            f"Disk {s['disk']:5.1f}%",
            f"Temp {temp}",
        ]
    else:
        raise ValueError(f"unknown page {page}")
    return PageContent(title, counter, rows)


def drift_offset(elapsed_s: float) -> tuple[int, int]:
    return _DRIFT[int(max(elapsed_s, 0) // 60) % len(_DRIFT)]


@functools.lru_cache(maxsize=1)
def load_font():
    try:
        return ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        return ImageFont.load_default()


def _clip(draw, text: str, font, max_px: float) -> str:
    """Trim text until it fits max_px wide, whatever font is loaded."""
    while text and draw.textlength(text, font=font) > max_px:
        text = text[:-1]
    return text


def draw_page(draw, content: PageContent, offset: tuple[int, int] = (0, 0)) -> None:
    ox, oy = offset
    font = load_font()
    margin = 2
    draw.rectangle((ox, oy, ox + WIDTH - 1, oy + TITLE_H - 1), fill="white")
    counter_w = draw.textlength(content.counter, font=font)
    title = _clip(draw, content.title, font, WIDTH - 3 * margin - counter_w)
    draw.text((ox + margin, oy), title, fill="black", font=font)
    draw.text((ox + WIDTH - margin - counter_w, oy), content.counter, fill="black", font=font)
    for i, row in enumerate(content.rows[:MAX_ROWS]):
        row = _clip(draw, row, font, WIDTH - 2 * margin)
        draw.text((ox + margin, oy + TITLE_H + 2 + i * ROW_H), row, fill="white", font=font)
