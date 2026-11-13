import http.client
import io
import json
import socket
import threading
import urllib.error

import containers
from containers import Container


def entry(name, state="running", status="Up 3 hours"):
    return {"Names": [f"/{name}"], "State": state, "Status": status}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(body: bytes, seen=None):
    def opener(url, timeout=None):
        if seen is not None:
            seen.append(url)
        return FakeResponse(body)

    return opener


def test_parse_unhealthy():
    (c,) = containers.parse_containers([entry("postgres", status="Up 3 hours (unhealthy)")])
    assert c == Container(name="postgres", running=True, health="unhealthy")
    assert containers.status_label(c) == "bad"


def test_parse_healthy():
    (c,) = containers.parse_containers([entry("coolify", status="Up 2 days (healthy)")])
    assert c.health == "healthy"
    assert containers.status_label(c) == "ok"


def test_parse_starting():
    (c,) = containers.parse_containers([entry("web", status="Up 5 seconds (health: starting)")])
    assert c.health == "starting"
    assert containers.status_label(c) == ".."


def test_parse_no_healthcheck():
    (c,) = containers.parse_containers([entry("oled", status="Up 2 days")])
    assert c.health is None
    assert containers.status_label(c) == "-"


def test_parse_exited_is_stopped():
    (c,) = containers.parse_containers([entry("job", state="exited", status="Exited (0) 2 hours ago")])
    assert c.running is False
    assert containers.status_label(c) == "stopped"


def test_parse_skips_malformed_entries():
    result = containers.parse_containers([{"State": "running"}, "junk", entry("ok-one")])
    assert [c.name for c in result] == ["ok-one"]


def test_unhealthy_names_returns_only_bad():
    cs = containers.parse_containers(
        [
            entry("a", status="Up (unhealthy)"),
            entry("b", status="Up (healthy)"),
            entry("c", state="exited", status="Exited (1) (unhealthy)"),
            entry("d", status="Up (unhealthy)"),
        ]
    )
    assert containers.unhealthy_names(cs) == frozenset({"a", "d"})


def test_unhealthy_names_none_passthrough():
    assert containers.unhealthy_names(None) is None


def test_fetch_requests_all_containers():
    seen = []
    body = json.dumps([entry("x")]).encode()
    result = containers.fetch_containers("http://127.0.0.1:2375", opener=opener_returning(body, seen))
    assert seen == ["http://127.0.0.1:2375/containers/json?all=1"]
    assert [c.name for c in result] == ["x"]


def test_fetch_returns_none_on_urlerror():
    def opener(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    assert containers.fetch_containers("http://x", opener=opener) is None


def test_fetch_returns_none_on_http_500():
    def opener(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    assert containers.fetch_containers("http://x", opener=opener) is None


def test_fetch_returns_none_on_timeout():
    def opener(url, timeout=None):
        raise TimeoutError("timed out")

    assert containers.fetch_containers("http://x", opener=opener) is None


def test_fetch_returns_none_on_malformed_json():
    assert containers.fetch_containers("http://x", opener=opener_returning(b"<html>nope")) is None


def test_fetch_returns_none_when_json_is_not_a_list():
    assert containers.fetch_containers("http://x", opener=opener_returning(b'{"message": "forbidden"}')) is None


def test_fetch_returns_none_on_http_protocol_errors():
    for exc in (http.client.IncompleteRead(b"[{"), http.client.BadStatusLine("garbage")):
        def opener(url, timeout=None, exc=exc):
            raise exc

        assert containers.fetch_containers("http://x", opener=opener) is None


def test_fetch_returns_none_on_truncated_body_from_a_real_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\nContent-Type: application/json\r\n\r\n[{")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.getsockname()[1]}"
        assert containers.fetch_containers(url, timeout=2) is None
    finally:
        server.close()


def test_fetch_tolerates_a_trailing_slash_in_the_base_url():
    seen = []
    body = json.dumps([entry("x")]).encode()
    containers.fetch_containers("http://127.0.0.1:2375/", opener=opener_returning(body, seen))
    assert seen == ["http://127.0.0.1:2375/containers/json?all=1"]
