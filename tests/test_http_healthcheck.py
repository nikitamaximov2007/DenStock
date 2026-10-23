"""The web healthcheck must finish before Docker's timeout without a shell child."""

import importlib.util
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker/http_healthcheck.py"
spec = importlib.util.spec_from_file_location("http_healthcheck", SCRIPT)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


class Handler(BaseHTTPRequestHandler):
    status = 200
    delay = 0

    def do_GET(self):
        if self.delay:
            time.sleep(self.delay)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


def _server(status=200, delay=0):
    handler = type("TestHandler", (Handler,), {"status": status, "delay": delay})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_compose_http_checks_execute_one_bounded_process():
    for file_name, services in (
        ("docker-compose.yml", ("web", "catalog-web")),
        ("docker-compose.emergency.yml", ("emergency-web",)),
    ):
        compose = yaml.safe_load((ROOT / file_name).read_text(encoding="utf-8"))
        for name in services:
            check = compose["services"][name]["healthcheck"]
            assert check["test"] == ["CMD", "python", "/app/docker/http_healthcheck.py"]
            assert check["timeout"] == "5s"
            assert check["interval"] == "15s"


def test_http_200_and_unhealthy_status():
    for status, expected in ((200, 0), (503, 1)):
        server = _server(status=status)
        try:
            assert healthcheck.main(server.server_port) == expected
        finally:
            server.shutdown()
            server.server_close()


def test_connection_refused():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    assert healthcheck.main(port) == 1


def test_hung_response_exits_before_docker_timeout():
    server = _server(delay=4)
    try:
        started = time.monotonic()
        assert healthcheck.main(server.server_port) == 1
        assert time.monotonic() - started < 4
    finally:
        server.shutdown()
        server.server_close()


def test_total_deadline_is_hard_limit(monkeypatch):
    class HungConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args):
            time.sleep(4)

        def close(self):
            pass

    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", HungConnection)
    started = time.monotonic()
    assert healthcheck.main() == 1
    assert time.monotonic() - started < 4
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
