#!/usr/bin/env bash
# Run only from Install-DenisStock-EmergencyWorkstation.ps1 as Administrator.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root inside the WSL distribution." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends docker.io docker-compose-v2 ca-certificates
systemctl enable --now docker

if [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
  usermod -aG docker "$SUDO_USER"
fi

echo "WSL Docker Engine is ready. Sign out of WSL and run the Windows installer again."
