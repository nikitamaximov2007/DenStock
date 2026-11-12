"""The web healthcheck must finish before Docker's timeout without a shell child."""

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
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
        self.server.hosts.append(self.headers.get("Host"))
        if self.delay:
            time.sleep(self.delay)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


def _server(status=200, delay=0):
    handler = type("TestHandler", (Handler,), {"status": status, "delay": delay})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.hosts = []
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
    for status, expected in ((200, 0), (400, 1), (503, 1)):
        server = _server(status=status)
        try:
            assert healthcheck.main(server.server_port) == expected
            assert server.hosts == ["localhost"]
        finally:
            server.shutdown()
            server.server_close()


@contextmanager
def _django_server(public, allowed_hosts):
    """Serve actual Django views and middleware with isolated in-memory SQLite."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "PUBLIC_", "DATABASE_URL", "DENSTOCK_"))
    }
    env.update(
        DJANGO_SETTINGS_MODULE="config.settings.test",
        DJANGO_SECRET_KEY="synthetic-healthcheck-test",
        HEALTHCHECK_TEST_PUBLIC="1" if public else "0",
        HEALTHCHECK_TEST_HOSTS=allowed_hosts,
        HEALTHCHECK_TEST_PORT=str(port),
    )
    script = """
import os
import django
from django.core.handlers.wsgi import WSGIHandler
from django.test import override_settings
from wsgiref.simple_server import make_server

django.setup()
settings = {'ALLOWED_HOSTS': os.environ['HEALTHCHECK_TEST_HOSTS'].split(','), 'DEBUG': False}
if os.environ['HEALTHCHECK_TEST_PUBLIC'] == '1':
    from apps.catalog.public_settings import PUBLIC_SETTINGS
    settings.update(PUBLIC_SETTINGS)
with override_settings(**settings):
    server = make_server('127.0.0.1', int(os.environ['HEALTHCHECK_TEST_PORT']), WSGIHandler())
    server.serve_forever()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode(errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("isolated Django HTTP server did not start")
        yield port
    finally:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)


def test_probe_with_real_django_views_and_public_host_policy():
    for public, hosts in (
        (False, "localhost,127.0.0.1"),
        (True, "pro-brp.ru,localhost"),
    ):
        with _django_server(public, hosts) as port:
            assert healthcheck.main(port) == 0
            connection = HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/healthz/", headers={"Host": "127.0.0.1"})
                assert connection.getresponse().status == (400 if public else 200)
            finally:
                connection.close()


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

        def request(self, *_args, **_kwargs):
            time.sleep(4)

        def close(self):
            pass

    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", HungConnection)
    started = time.monotonic()
    assert healthcheck.main() == 1
    assert time.monotonic() - started < 4
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
