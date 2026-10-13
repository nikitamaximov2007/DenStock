"""Qualification of the generation layout (docs/design/dr-generation-storage.md, A).

Real Docker: one named volume, an app container (UID 2001) that owns
``gen-<id>/`` directories and a ``current`` symlink, and a real Caddy 2 that
mounts the SAME volume read-only at a DIFFERENT path and serves
``/srv/media-volume/current`` behind the private-attachment guard. Synthetic
data only; every container, network and volume is removed afterwards.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request

import pytest


def _docker_ok():
    if not shutil.which("docker"):
        return False
    for image in ("caddy:2", "python:3.12-alpine"):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
            return False
    return True


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="Docker with caddy:2 and python required")

CADDYFILE = """{
\tauto_https off
\tadmin off
}
:80 {
\thandle_path /media/* {
\t\t@private_attachments path_regexp private_attachments (?i)(^|/|%2f)customer_requests(/|%2f|$)
\t\trespond @private_attachments 404
\t\troot * /srv/media-volume/current
\t\tfile_server
\t}
}
"""

INIT = r"""
import os
root = "/app/media-volume"
for gen in ("gen-1", "gen-2"):
    os.makedirs(f"{root}/{gen}/part-items", exist_ok=True)
    os.makedirs(f"{root}/{gen}/customer_requests/1", exist_ok=True)
    open(f"{root}/{gen}/part-items/photo.jpg", "w").write(gen.upper() + "-PHOTO")
    open(f"{root}/{gen}/customer_requests/1/scan.pdf", "w").write("PRIVATE")
    open(f"{root}/{gen}/.denstock-generation", "w").write(gen)
os.symlink("/etc/hostname", f"{root}/gen-2/part-items/escape.txt")  # a link INSIDE a generation
os.symlink("gen-1", f"{root}/current")                              # RELATIVE target
os.symlink(f"{root}/gen-1", f"{root}/current-absolute")             # ABSOLUTE target (trap)
for dirpath, dirnames, filenames in os.walk(root):
    for name in dirnames + filenames:
        os.lchown(os.path.join(dirpath, name), 2001, 2001)
os.chown(root, 2001, 2001)
"""


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("genq")
    name = "genq-" + secrets.token_hex(4)
    volume, net = name + "-vol", name + "-net"
    caddyfile = tmp / "Caddyfile"
    caddyfile.write_text(CADDYFILE)
    _docker("volume", "create", volume)
    _docker("network", "create", net)
    try:
        _docker("run", "--rm", "-v", f"{volume}:/app/media-volume", "python:3.12-alpine",
                "python", "-c", INIT)
        _docker("run", "-d", "--name", f"{name}-app", "--user", "2001:2001",
                "-v", f"{volume}:/app/media-volume", "python:3.12-alpine", "sleep", "3600")
        _docker("run", "-d", "--name", f"{name}-proxy", "--network", net, "-p", "127.0.0.1::80",
                "-v", f"{volume}:/srv/media-volume:ro",
                "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro",
                "caddy:2")
        port = json.loads(_docker("inspect", f"{name}-proxy").stdout)[0]["NetworkSettings"][
            "Ports"]["80/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/media/part-items/photo.jpg", timeout=2)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        yield {"base": base, "app": f"{name}-app", "volume": volume}
    finally:
        _docker("rm", "-f", f"{name}-app", f"{name}-proxy", check=False)
        _docker("network", "rm", net, check=False)
        _docker("volume", "rm", "-f", volume, check=False)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _app(stack, code):
    return _docker("exec", stack["app"], "python", "-c", code).stdout.strip()


SWITCH = r"""
import os, sys
root = "/app/media-volume"
target = sys.argv[1] if len(sys.argv) > 1 else None
def switch(gen):
    tmp = f"{root}/.current.{os.getpid()}"
    os.symlink(gen, tmp)
    os.replace(tmp, f"{root}/current")   # rename(2): atomic replacement of the link
    fd = os.open(root, os.O_RDONLY); os.fsync(fd); os.close(fd)
"""


def test_caddy_serves_through_the_relative_current_link_and_keeps_the_guard(stack):
    assert _get(stack["base"] + "/media/part-items/photo.jpg") == (200, b"GEN-1-PHOTO")
    assert _get(stack["base"] + "/media/customer_requests/1/scan.pdf")[0] == 404


def test_an_absolute_link_target_breaks_in_the_other_container(stack):
    """The app resolves /app/media-volume/gen-1; Caddy mounts the volume at
    /srv/media-volume, where that absolute target does not exist. The link must
    be RELATIVE (``current -> gen-<id>``)."""
    probe = "import os; print(os.path.isdir('/app/media-volume/current-absolute'))"
    inside_app = _app(stack, probe)
    assert inside_app == "True"
    proxy = stack["app"].replace("-app", "-proxy")
    seen_by_proxy = _docker("exec", proxy, "sh", "-c",
                            "test -d /srv/media-volume/current-absolute && echo yes || echo no")
    assert seen_by_proxy.stdout.strip() == "no"


def test_atomic_switch_under_load_never_returns_an_error(stack):
    results, stop = [], threading.Event()

    def hammer():
        while not stop.is_set():
            results.append(_get(stack["base"] + "/media/part-items/photo.jpg"))

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for thread in threads:
        thread.start()
    _app(stack, SWITCH + "\nimport time\nfor i in range(60):\n"
         "    switch('gen-2' if i % 2 == 0 else 'gen-1'); time.sleep(0.01)\nswitch('gen-1')")
    stop.set()
    for thread in threads:
        thread.join()
    assert len(results) > 100
    bodies = {body for status, body in results if status == 200}
    assert {status for status, _ in results} == {200}, {s for s, _ in results}
    assert bodies <= {b"GEN-1-PHOTO", b"GEN-2-PHOTO"} and len(bodies) == 2


def test_open_files_and_writes_in_flight_stay_in_the_old_generation(stack):
    """A reader opened before the switch keeps reading the OLD generation, and a
    write that was opened before the switch lands in the OLD generation, so it
    is missing from the new one: writers (web, bots) must be stopped first."""
    out = _app(stack, SWITCH + r"""
reader = open(f"{root}/current/part-items/photo.jpg")
writer = open(f"{root}/current/part-items/in-flight.txt", "w")
switch("gen-2")
writer.write("late"); writer.close()
print(reader.read())
print(os.path.exists(f"{root}/gen-1/part-items/in-flight.txt"),
      os.path.exists(f"{root}/gen-2/part-items/in-flight.txt"))
print(os.path.realpath(f"{root}/current"))
os.remove(f"{root}/gen-1/part-items/in-flight.txt")
switch("gen-1")
""")
    assert out.splitlines() == ["GEN-1-PHOTO", "True False", "/app/media-volume/gen-2"]


def test_a_link_inside_a_generation_is_served_by_caddy(stack):
    """Caddy's file_server follows a symlink placed inside a generation. Nothing
    in DenisStock creates links (create_archive refuses them), but a restore or
    migration must verify the generation is link-free before switching."""
    _app(stack, SWITCH + "\nswitch('gen-2')")
    try:
        status, body = _get(stack["base"] + "/media/part-items/escape.txt")
    finally:
        _app(stack, SWITCH + "\nswitch('gen-1')")
    assert status == 200 and body.strip(), "Caddy follows links inside the served root"


def test_backup_must_archive_the_realpath_not_the_link(tmp_path):
    root = tmp_path / "media-volume"
    (root / "gen-7" / "part-items").mkdir(parents=True)
    (root / "gen-7" / "part-items" / "a.jpg").write_bytes(b"A")
    (root / "current").symlink_to("gen-7")
    with tarfile.open(tmp_path / "naive.tar", "w") as tar:
        tar.add(root / "current", arcname=".")
    with tarfile.open(tmp_path / "naive.tar") as tar:
        assert [m.type for m in tar.getmembers()] == [tarfile.SYMTYPE]  # only the link
    with tarfile.open(tmp_path / "real.tar", "w") as tar:
        tar.add((root / "current").resolve(), arcname=".")
    with tarfile.open(tmp_path / "real.tar") as tar:
        assert "./part-items/a.jpg" in tar.getnames()
