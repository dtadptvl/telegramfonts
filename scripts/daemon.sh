#!/usr/bin/env bash
# TeleFont A23 Worker Daemon Supervisor
# Ensures persistent background operation across SSH disconnects and automatic restart on transient exits.

set -u

if [ -d "${HOME}/telefont" ]; then
  ROOT_DIR="${HOME}/telefont"
else
  ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi

if [ -d "/data/data/com.termux/files/usr/tmp" ]; then
  LOG_FILE="/data/data/com.termux/files/usr/tmp/telefont.log"
else
  LOG_FILE="/tmp/telefont.log"
fi

mkdir -p "$(dirname "${LOG_FILE}")"
cd "${ROOT_DIR}"

if [ -f "${HOME}/.telefont.env" ]; then
  set -a
  source "${HOME}/.telefont.env"
  set +a
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] TeleFont daemon supervisor started in ${ROOT_DIR}" >> "${LOG_FILE}"

while true; do
  export PYTHONPATH="${ROOT_DIR}/agent/src:${PYTHONPATH:-}"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Launching python agent/src/main.py..." >> "${LOG_FILE}"
  python "${ROOT_DIR}/agent/src/main.py" >> "${LOG_FILE}" 2>&1
  EXIT_CODE=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Agent process exited with code ${EXIT_CODE}. Restarting in 5s..." >> "${LOG_FILE}"
  sleep 5
done