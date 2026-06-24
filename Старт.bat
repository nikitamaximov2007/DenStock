@echo off
chcp 65001 >nul
echo === Запуск DenStock ===
docker compose up -d --build
if errorlevel 1 (
  echo Не удалось запустить. Установлен и запущен ли Docker Desktop?
  pause
  exit /b 1
)
echo Открываю http://localhost ...
start "" http://localhost
