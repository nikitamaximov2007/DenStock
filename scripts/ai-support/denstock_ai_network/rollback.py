import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .constants import FIREWALL_TABLE, NFTABLES_CONFIG_PATH
from .installer import (
    CODEX_BINARY,
    CODEX_MARKER,
    INSTALL_ROOT,
    SYSTEMD_ROOT,
    TMPFILES_ROOT,
    UNIT_FILES,
    WRAPPERS,
    InstallationError,
    verify_codex_install_marker,
    verify_installed_codex_binary,
)


class RollbackError(RuntimeError):
    pass


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def rollback_plan() -> list[str]:
    return [
        "disable and stop denstock-ai-launcher.socket",
        "disable and stop denstock-ai-proxy.service",
        "stop and verify all denstock-ai-launcher@ instances",
        "disable and stop denstock-ai-firewall.service",
        f"delete nftables table inet {FIREWALL_TABLE} if it remains",
        "remove only the pinned Codex binary, units, wrappers, code, docs and tmpfiles rule",
        "preserve /etc/denstock-ai, CODEX_HOME and users",
        "run systemctl daemon-reload",
    ]


def apply_rollback(runner=_run) -> None:
    marker_present = CODEX_MARKER.exists() or CODEX_MARKER.is_symlink()
    binary_present = CODEX_BINARY.exists() or CODEX_BINARY.is_symlink()
    remove_codex = marker_present and binary_present
    if marker_present and not binary_present:
        raise RollbackError("refusing rollback with an incomplete managed Codex installation")
    if remove_codex:
        try:
            verify_installed_codex_binary()
            verify_codex_install_marker()
        except InstallationError as exc:
            raise RollbackError("refusing to remove an unrecognized Codex binary") from exc

    def disable_and_verify(unit: str) -> None:
        runner(["/usr/bin/systemctl", "disable", "--now", unit])
        active = runner(["/usr/bin/systemctl", "is-active", "--quiet", unit])
        if active.returncode == 0:
            raise RollbackError(f"refusing rollback while {unit} remains active")

    disable_and_verify("denstock-ai-launcher.socket")
    disable_and_verify("denstock-ai-proxy.service")
    launcher_instances = "denstock-ai-launcher@*.service"
    runner(["/usr/bin/systemctl", "stop", launcher_instances])
    active_instances = runner(
        ["/usr/bin/systemctl", "is-active", "--quiet", launcher_instances]
    )
    if active_instances.returncode == 0:
        raise RollbackError("refusing rollback while a launcher instance remains active")
    disable_and_verify("denstock-ai-firewall.service")
    listed = runner(["/usr/sbin/nft", "list", "table", "inet", FIREWALL_TABLE])
    if listed.returncode == 0:
        deleted = runner(["/usr/sbin/nft", "delete", "table", "inet", FIREWALL_TABLE])
        if deleted.returncode != 0:
            raise RollbackError("nftables table could not be removed")
    try:
        NFTABLES_CONFIG_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise RollbackError("nftables runtime policy could not be removed") from exc
    for name in UNIT_FILES:
        (SYSTEMD_ROOT / name).unlink(missing_ok=True)
    for name in WRAPPERS:
        (Path("/usr/local/sbin") / name).unlink(missing_ok=True)
    if remove_codex:
        CODEX_BINARY.unlink()
    (TMPFILES_ROOT / "denstock-ai.conf").unlink(missing_ok=True)
    Path("/usr/local/share/doc/denstock-ai/ai-support-maxinik-network.md").unlink(
        missing_ok=True
    )
    expected_install_root = Path("/usr/local/lib/denstock-ai")
    if INSTALL_ROOT.resolve() != expected_install_root:
        raise RollbackError("refusing unsafe recursive removal")
    try:
        install_info = INSTALL_ROOT.lstat()
    except FileNotFoundError:
        install_info = None
    except OSError as exc:
        raise RollbackError("installed code could not be inspected") from exc
    if install_info is not None:
        if not INSTALL_ROOT.is_dir() or INSTALL_ROOT.is_symlink():
            raise RollbackError("refusing unsafe recursive removal")
        try:
            shutil.rmtree(INSTALL_ROOT)
        except OSError as exc:
            raise RollbackError("installed code could not be removed") from exc
    result = runner(["/usr/bin/systemctl", "daemon-reload"])
    if result.returncode != 0:
        raise RollbackError("systemd daemon-reload failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rollback the DenisStock AI host isolation layer")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(rollback_plan()))
        return 0
    if os.name != "posix" or os.geteuid() != 0:
        print("rollback_error: Linux root is required", file=sys.stderr)
        return 2
    try:
        apply_rollback()
    except RollbackError as exc:
        print(f"rollback_error: {exc}", file=sys.stderr)
        return 2
    print("rollback complete; secrets, state and users were preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
