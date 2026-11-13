from __future__ import annotations

import http.client
import json
import re
import urllib.request
from dataclasses import dataclass

_HEALTH = re.compile(r"\((unhealthy|healthy|health: starting)\)")


@dataclass(frozen=True)
class Container:
    name: str
    running: bool
    health: str | None  # "healthy" | "unhealthy" | "starting" | None (no healthcheck)


def _health(status: str) -> str | None:
    m = _HEALTH.search(status)
    if not m:
        return None
    return "starting" if m.group(1) == "health: starting" else m.group(1)


def parse_containers(payload: list) -> list[Container]:
    result = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        names = item.get("Names")
        if not names or not isinstance(names, list) or not isinstance(names[0], str):
            continue
        running = item.get("State") == "running"
        health = _health(str(item.get("Status", ""))) if running else None
        result.append(Container(name=names[0].lstrip("/"), running=running, health=health))
    return result


def status_label(c: Container) -> str:
    if not c.running:
        return "stopped"
    return {"healthy": "ok", "unhealthy": "bad", "starting": ".."}.get(c.health, "-")


def unhealthy_names(containers: list[Container] | None) -> frozenset[str] | None:
    if containers is None:
        return None
    return frozenset(c.name for c in containers if c.health == "unhealthy")


def fetch_containers(
    base_url: str, opener=urllib.request.urlopen, timeout: float = 2.0
) -> list[Container] | None:
    """List containers through the read-only Docker proxy; None if anything goes wrong."""
    try:
        with opener(f"{base_url.rstrip('/')}/containers/json?all=1", timeout=timeout) as resp:
            payload = json.load(resp)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if not isinstance(payload, list):
        return None
    return parse_containers(payload)
