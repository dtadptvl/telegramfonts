#!/usr/bin/env bash
# Canonical D12 A23 worker boundary.
# Termux owns this supervisor; the worker process is always started inside the
# explicitly named Debian release and runtime.

set -u -o pipefail

STARTUP_FD=""
if [ "$#" -gt 0 ]; then
  if [ "$#" -ne 2 ] || [ "$1" != "--ready-fd" ]; then
    printf 'telefont-debian-supervisor: unsupported arguments\n' >&2
    exit 1
  fi
  case "$2" in
    ''|*[!0-9]*)
      printf 'telefont-debian-supervisor: startup fd must be numeric\n' >&2
      exit 1
      ;;
  esac
  STARTUP_FD="$2"
fi

notify_startup() {
  local status="$1"
  if [ -n "$STARTUP_FD" ]; then
    if ! printf '%s\n' "$status" >&"$STARTUP_FD" 2>/dev/null; then
      STARTUP_FD=""
      return 1
    fi
    STARTUP_FD=""
  fi
}

fail() {
  notify_startup FAIL || true
  printf 'telefont-debian-supervisor: %s\n' "$1" >&2
  exit 1
}

readonly RELEASE_SHA="582dba833bf4f955e872823b99ee24a57fde21b3"
readonly DEBIAN_ROOT="/data/local/chroot/debian"
readonly CHROOT_BIN="/data/data/com.termux/files/usr/bin/chroot"
readonly RELEASE_ROOT="/opt/telefont-release-$RELEASE_SHA"
readonly RELEASE_ARCHIVE="/opt/telefont-release-$RELEASE_SHA.tar"
readonly RELEASE_ARCHIVE_SHA256="02363eac3ea3f611bcbd94608c15379fa2b0f6c0d4286c244f83ba9122ef24ab"
readonly RUNTIME_ROOT="/opt/telefont-runtime-$RELEASE_SHA"
readonly RUNTIME_MANIFEST="/opt/telefont-runtime-$RELEASE_SHA.manifest"
readonly WORKER_ENTRYPOINT="$RELEASE_ROOT/agent/src/main.py"
readonly RUNTIME_PYTHON="$RUNTIME_ROOT/bin/python"
readonly ARCHIVE_ROOT="/srv/fontlab/archive"
readonly TERMUX_PREFIX="/data/data/com.termux/files/usr"
readonly CONFIG_SOURCE_DEFAULT="/data/data/com.termux/files/home/.telefont.env"
readonly LOCK_FILE="$TERMUX_PREFIX/var/run/telefont-debian-worker.lock"
readonly LOG_FILE="$TERMUX_PREFIX/tmp/telefont-debian-worker.log"
readonly MAX_RESTARTS=3
readonly RESTART_WINDOW_SECONDS=300
readonly RESTART_DELAY_SECONDS=5
readonly HOST_PATH="$PATH"
readonly FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
readonly WORKER_PATH="$RUNTIME_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
readonly EXPECTED_RUNTIME_FINGERPRINT="python=3.11.2|arch=aarch64|annotated-types=0.8.0|anyio=4.14.2|brotli=1.2.0|certifi=2026.7.22|fonttools=4.63.0|freetype-py=2.5.1|h11=0.16.0|httpcore=1.0.9|httpx=0.28.1|idna=3.19|numpy=2.2.3|pillow=12.2.0|pip=26.1.2|pydantic=2.13.4|pydantic-core=2.46.4|pydantic-settings=2.15.0|python-dotenv=1.2.3|scipy=1.15.2|typing-extensions=4.16.0|typing-inspection=0.4.4|uharfbuzz=0.56.0|websockets=17.0.1"

fail_if_not_regular() {
  local path="$1"
  local label="$2"
  [ -f "$path" ] || fail "$label is unavailable"
  [ ! -L "$path" ] || fail "$label must not be a symlink"
}

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG_FILE"
}

[ -d "$DEBIAN_ROOT" ] || fail "Debian root is unavailable"
[ -x "$CHROOT_BIN" ] || fail "Debian chroot boundary is unavailable"
[ -d "$DEBIAN_ROOT$RELEASE_ROOT" ] || fail "required release identity is unavailable"
[ ! -L "$DEBIAN_ROOT$RELEASE_ROOT" ] || fail "release identity must not be a symlink"
[ -f "$DEBIAN_ROOT$WORKER_ENTRYPOINT" ] || fail "worker entrypoint is unavailable"
[ -d "$DEBIAN_ROOT$RUNTIME_ROOT" ] || fail "required runtime identity is unavailable"

# The clean release archive is content-addressed and compared with the staged
# tree. A SHA-named directory alone is not a release identity.
fail_if_not_regular "$DEBIAN_ROOT$RELEASE_ARCHIVE" "release identity archive"
release_archive_hash="$("$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/sha256sum "$RELEASE_ARCHIVE" 2>/dev/null)" \
  || fail "release identity archive cannot be hashed"
read -r release_archive_hash _ <<<"$release_archive_hash"
[ "$release_archive_hash" = "$RELEASE_ARCHIVE_SHA256" ] \
  || fail "release identity archive hash mismatch"
"$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/tar -d -f "$RELEASE_ARCHIVE" -C "$RELEASE_ROOT" \
  >/dev/null 2>&1 || fail "staged release contents differ from the clean release"

# The runtime identity is checked by architecture, interpreter version, and
# the exact bounded package set/versions produced by the accepted ARM64 stage.
# No worker code is started by this probe.
fail_if_not_regular "$DEBIAN_ROOT$RUNTIME_MANIFEST" "runtime identity manifest"
if ! "$CHROOT_BIN" "$DEBIAN_ROOT" /bin/sh -s -- "$RUNTIME_ROOT" "$RUNTIME_MANIFEST" "$EXPECTED_RUNTIME_FINGERPRINT" \
  >/dev/null 2>&1 <<'EOF'
set -u
runtime_root="$1"
manifest="$2"
expected="$3"
grep -Fx "# telefont-runtime-fingerprint=$expected" "$manifest" >/dev/null
cd "$runtime_root"
/usr/bin/sha256sum -c "$manifest"
EOF
then
  fail "Debian runtime contents differ from the identity manifest"
fi
"$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/test -x "$RUNTIME_PYTHON" \
  >/dev/null 2>&1 || fail "Debian runtime Python is unavailable"
runtime_fingerprint="$(
  HOME="/root" PYTHONPATH="" PATH="$WORKER_PATH" \
  "$CHROOT_BIN" "$DEBIAN_ROOT" "$RUNTIME_PYTHON" -c '
import importlib.metadata as metadata
import platform
import sys

names = (
    "annotated-types", "anyio", "brotli", "certifi", "fonttools", "freetype-py",
    "h11", "httpcore", "httpx", "idna", "numpy", "pillow", "pip", "pydantic",
    "pydantic-core", "pydantic-settings", "python-dotenv", "scipy",
    "typing-extensions", "typing-inspection", "uharfbuzz", "websockets",
)
installed = {
    str(dist.metadata.get("Name", "")).lower()
    for dist in metadata.distributions()
    if dist.metadata.get("Name")
}
if installed != set(names):
    raise SystemExit(2)
values = [
    "python=" + ".".join(str(part) for part in sys.version_info[:3]),
    "arch=" + platform.machine().lower(),
]
values.extend(name + "=" + metadata.version(name) for name in names)
print("|".join(values), end="")
' 2>/dev/null
)" || fail "Debian runtime fingerprint cannot be read"
[ "$runtime_fingerprint" = "$EXPECTED_RUNTIME_FINGERPRINT" ] \
  || fail "Debian runtime identity mismatch"

# Require an actual mount entry at the canonical path, and require ext4 on a
# device different from Debian /. This rejects an unmounted backing directory
# without depending on optional findmnt/blkid packages or printing device IDs.
if ! "$CHROOT_BIN" "$DEBIAN_ROOT" /bin/sh -s -- "$ARCHIVE_ROOT" \
  >/dev/null 2>&1 <<'EOF'
set -u
target="$1"
awk -v target="$target" '
  $5 == target {
    for (i = 6; i <= NF; i++) {
      if ($i == "-") {
        if ($(i + 1) == "ext4") {
          found = 1
        }
        exit
      }
    }
  }
  END { exit(found ? 0 : 1) }
' /proc/self/mountinfo
EOF
then
  fail "canonical archive is not an ext4 mount"
fi

archive_device="$("$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/stat -c %d "$ARCHIVE_ROOT" 2>/dev/null)" \
  || fail "canonical archive device cannot be read"
root_device="$("$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/stat -c %d / 2>/dev/null)" \
  || fail "Debian root device cannot be read"
[ -n "$archive_device" ] && [ "$archive_device" != "$root_device" ] \
  || fail "canonical archive resolves to the Debian root device"

[ -n "$FLOCK_BIN" ] || fail "flock is unavailable"

CONFIG_SOURCE="$(printenv TELEFONT_ENV_FILE 2>/dev/null || true)"
if [ -z "$CONFIG_SOURCE" ]; then
  CONFIG_SOURCE="$CONFIG_SOURCE_DEFAULT"
fi
[ -f "$CONFIG_SOURCE" ] || fail "canonical environment file is unavailable"

# Load the existing host-side config without ever echoing its contents. The
# worker receives the exported values through chroot; its HOME is reset below
# so it cannot fall back to a Termux dotfile or a dirty checkout.
set -a
. "$CONFIG_SOURCE" >/dev/null 2>&1
CONFIG_STATUS=$?
set +a
[ "$CONFIG_STATUS" -eq 0 ] || fail "canonical environment file could not be loaded"

export PATH="$HOST_PATH"
mkdir -p "$(dirname "$LOCK_FILE")" || fail "supervisor lock directory is unavailable"
mkdir -p "$(dirname "$LOG_FILE")" || fail "supervisor log directory is unavailable"

exec 9>"$LOCK_FILE" || fail "supervisor lock is unavailable"
"$FLOCK_BIN" -n 9 || fail "another Debian worker supervisor already owns the lock"

cd "$DEBIAN_ROOT" || fail "cannot enter Debian root"

log "started release=$RELEASE_SHA" || fail "supervisor log is unavailable"
notify_startup READY || fail "startup handshake is unavailable"

worker_pid=""
stop_requested=0

forward_stop() {
  stop_requested=1
  if [ -n "$worker_pid" ]; then
    kill -TERM "$worker_pid" 2>/dev/null || true
  fi
}

trap forward_stop INT TERM HUP

restart_count=0
window_start=0

run_worker() {
  HOME="/root" \
  PATH="$WORKER_PATH" \
  PYTHONPATH="$RELEASE_ROOT/agent/src" \
  FONT_ARCHIVE_ROOT="$ARCHIVE_ROOT" \
  "$CHROOT_BIN" "$DEBIAN_ROOT" "$RUNTIME_PYTHON" "$WORKER_ENTRYPOINT"
}

while :; do
  [ "$stop_requested" -eq 0 ] || break

  log "launching Debian worker"
  run_worker </dev/null >>"$LOG_FILE" 2>&1 &
  worker_pid=$!
  wait "$worker_pid"
  exit_code=$?
  worker_pid=""

  [ "$stop_requested" -eq 0 ] || break

  now="$(date -u +%s)" || fail "clock is unavailable for restart bounding"
  if [ "$window_start" -eq 0 ] || [ $((now - window_start)) -ge "$RESTART_WINDOW_SECONDS" ]; then
    window_start="$now"
    restart_count=0
  fi

  restart_count=$((restart_count + 1))
  if [ "$restart_count" -gt "$MAX_RESTARTS" ]; then
    log "restart budget exhausted after exit=$exit_code"
    exit 1
  fi

  log "worker exited with code=$exit_code; restart=$restart_count/$MAX_RESTARTS"
  sleep "$RESTART_DELAY_SECONDS"
done

log "stopped"
exit 0
