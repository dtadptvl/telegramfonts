#!/usr/bin/env bash
# Canonical D12 A23 worker boundary.
# Termux owns this supervisor; the worker process is always started inside the
# explicitly named Debian release and runtime.

set -u

readonly RELEASE_SHA="582dba833bf4f955e872823b99ee24a57fde21b3"
readonly DEBIAN_ROOT="/data/local/chroot/debian"
readonly CHROOT_BIN="/data/data/com.termux/files/usr/bin/chroot"
readonly RELEASE_ROOT="/opt/telefont-release-${RELEASE_SHA}"
readonly RUNTIME_ROOT="/opt/telefont-runtime-${RELEASE_SHA}"
readonly WORKER_ENTRYPOINT="${RELEASE_ROOT}/agent/src/main.py"
readonly RUNTIME_PYTHON="${RUNTIME_ROOT}/bin/python"
readonly ARCHIVE_ROOT="/srv/fontlab/archive"
readonly TERMUX_PREFIX="/data/data/com.termux/files/usr"
readonly CONFIG_SOURCE_DEFAULT="/data/data/com.termux/files/home/.telefont.env"
readonly LOCK_FILE="${TERMUX_PREFIX}/var/run/telefont-debian-worker.lock"
readonly LOG_FILE="${TERMUX_PREFIX}/tmp/telefont-debian-worker.log"
readonly MAX_RESTARTS=3
readonly RESTART_WINDOW_SECONDS=300
readonly RESTART_DELAY_SECONDS=5
readonly HOST_PATH="${PATH:-/data/data/com.termux/files/usr/bin:/system/bin}"
readonly FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
readonly WORKER_PATH="${RUNTIME_ROOT}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

fail() {
  printf 'telefont-debian-supervisor: %s\n' "$1" >&2
  exit 1
}

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"${LOG_FILE}"
}

[ -d "${DEBIAN_ROOT}" ] || fail "Debian root is unavailable"
[ -x "${CHROOT_BIN}" ] || fail "Debian chroot boundary is unavailable"
[ -d "${DEBIAN_ROOT}${RELEASE_ROOT}" ] || fail "required release identity is unavailable"
[ ! -L "${DEBIAN_ROOT}${RELEASE_ROOT}" ] || fail "release identity must not be a symlink"
[ -f "${DEBIAN_ROOT}${WORKER_ENTRYPOINT}" ] || fail "worker entrypoint is unavailable"
[ -d "${DEBIAN_ROOT}${RUNTIME_ROOT}" ] || fail "required runtime identity is unavailable"

# The runtime's venv links resolve inside Debian, so validate it through the
# chroot instead of following its links from the Termux host filesystem.
"${CHROOT_BIN}" "${DEBIAN_ROOT}" /usr/bin/test -x "${RUNTIME_PYTHON}" \
  >/dev/null 2>&1 || fail "Debian runtime Python is unavailable"

[ -d "${DEBIAN_ROOT}${ARCHIVE_ROOT}" ] || fail "canonical archive path is unavailable"
[ ! -L "${DEBIAN_ROOT}${ARCHIVE_ROOT}" ] || fail "canonical archive path must not be a symlink"

[ -n "${FLOCK_BIN}" ] || fail "flock is unavailable"

CONFIG_SOURCE="${TELEFONT_ENV_FILE:-${CONFIG_SOURCE_DEFAULT}}"
[ -f "${CONFIG_SOURCE}" ] || fail "canonical environment file is unavailable"

# Load the existing host-side config without ever echoing its contents. The
# worker receives the exported values through chroot; its HOME is reset below
# so it cannot fall back to a Termux dotfile or a dirty checkout.
set -a
. "${CONFIG_SOURCE}" >/dev/null 2>&1
CONFIG_STATUS=$?
set +a
[ "${CONFIG_STATUS}" -eq 0 ] || fail "canonical environment file could not be loaded"

export PATH="${HOST_PATH}"
mkdir -p "$(dirname "${LOCK_FILE}")" || fail "supervisor lock directory is unavailable"
mkdir -p "$(dirname "${LOG_FILE}")" || fail "supervisor log directory is unavailable"

exec 9>"${LOCK_FILE}" || fail "supervisor lock is unavailable"
"${FLOCK_BIN}" -n 9 || fail "another Debian worker supervisor already owns the lock"

cd "${DEBIAN_ROOT}" || fail "cannot enter Debian root"

log "started release=${RELEASE_SHA}"

worker_pid=""
stop_requested=0

forward_stop() {
  stop_requested=1
  if [ -n "${worker_pid:-}" ]; then
    kill -TERM "${worker_pid}" 2>/dev/null || true
  fi
}

trap forward_stop INT TERM HUP

restart_count=0
window_start=0

run_worker() {
  HOME="/root" \
  PATH="${WORKER_PATH}" \
  PYTHONPATH="${RELEASE_ROOT}/agent/src" \
  FONT_ARCHIVE_ROOT="${ARCHIVE_ROOT}" \
  "${CHROOT_BIN}" "${DEBIAN_ROOT}" "${RUNTIME_PYTHON}" "${WORKER_ENTRYPOINT}"
}

while :; do
  [ "${stop_requested}" -eq 0 ] || break

  log "launching Debian worker"
  run_worker </dev/null >>"${LOG_FILE}" 2>&1 &
  worker_pid=$!
  wait "${worker_pid}"
  exit_code=$?
  worker_pid=""

  [ "${stop_requested}" -eq 0 ] || break

  now="$(date -u +%s)" || fail "clock is unavailable for restart bounding"
  if [ "${window_start}" -eq 0 ] || [ $((now - window_start)) -ge "${RESTART_WINDOW_SECONDS}" ]; then
    window_start="${now}"
    restart_count=0
  fi

  restart_count=$((restart_count + 1))
  if [ "${restart_count}" -gt "${MAX_RESTARTS}" ]; then
    log "restart budget exhausted after exit=${exit_code}"
    exit 1
  fi

  log "worker exited with code=${exit_code}; restart=${restart_count}/${MAX_RESTARTS}"
  sleep "${RESTART_DELAY_SECONDS}"
done

log "stopped"
exit 0
