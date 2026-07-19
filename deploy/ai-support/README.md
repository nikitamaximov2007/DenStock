# DenisStock AI host isolation assets

This directory contains templates only. It does not contain MAXINIK credentials,
Codex authentication, a VLESS URI, or an enabled production configuration.

Pinned components:

- sing-box `1.13.14` for Linux amd64;
- official release asset `sing-box_1.13.14_linux_amd64.deb`;
- SHA-256 `320523f9586877c4cb244df753d848356787e15f2f4e23a00908af2422206542`;
- Codex CLI `0.142.5`;
- official native Codex asset `codex-x86_64-unknown-linux-musl.tar.gz`;
- Codex SHA-256 `cb933ec3cb61bf4b5fc88eecf5e6149829faa6172535b6ef0afb0154beb4aab8`;
- extracted Codex binary SHA-256
  `ac06f492f3ded7a8e2f36dc961e3cc5276a3c4841a2695d4681d0557c5b30e41`;
- launcher protocol `1`, launcher `1.0.0`;
- local mixed HTTP/SOCKS proxy `127.0.0.1:2080` by default.

The installer verifies the pinned sing-box DEB and Codex archive. It extracts
only the sing-box binary with `dpkg-deb` and exactly one regular native Codex
binary with Python's tar reader. It does not run package maintainer scripts or
install the vendor service.

The installer has an offline `--dry-run` mode and never enables or starts a
service. Explicit `denstock-ai-install`, `denstock-ai-update`,
`denstock-ai-verify`, and `denstock-ai-rollback` commands are installed on the
host. Read `docs/operations/ai-support-maxinik-network.md` before using any
apply or systemd command.
