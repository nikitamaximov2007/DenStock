#!/usr/bin/env bash
# Run only from Install-DenisStock-EmergencyWorkstation.ps1 as Administrator.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root inside the WSL distribution." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# Docker внутри WSL поднимается через systemd, а systemd поддерживает только
# современный WSL из Microsoft Store, не встроенный компонент Windows 10.
#
# Раньше при отсутствии systemd сценарий просто прописывал его в /etc/wsl.conf
# и просил перезапустить WSL. Если версия WSL его не поддерживает, настройка
# игнорируется, и человек у компьютера попадает в круг: перезапуск, повтор,
# то же сообщение. Поэтому вторая попытка отличается от первой.
if ! systemctl --version >/dev/null 2>&1; then
  if grep -qs '^systemd=true' /etc/wsl.conf; then
    cat >&2 <<'EOF'
systemd уже включён в /etc/wsl.conf, но не работает.
Это значит, что установленная версия WSL его не поддерживает.
Выполните в Windows от имени администратора:
    wsl --update
    wsl --shutdown
затем повторите установку. Если wsl --update сообщает, что команда неизвестна,
установите WSL из Microsoft Store: встроенный компонент Windows 10 systemd не
поддерживает.
EOF
    exit 43
  fi
  cat >/etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
  echo "WSL systemd was enabled. Run 'wsl --shutdown' from Windows and rerun provisioning." >&2
  exit 42
fi

# Повторный запуск безопасен: пакеты не переустанавливаются, если уже стоят.
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends docker.io docker-compose-v2 ca-certificates
fi
systemctl enable --now docker

if [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
  usermod -aG docker "$SUDO_USER"
fi

echo "WSL Docker Engine is ready. Sign out of WSL and run the Windows installer again."
