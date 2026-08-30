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
readonly HOST_ARCHIVE_BRIDGE="/data/data/com.termux/files/home/telefont-archive-bridge"
readonly STAT_BIN="$TERMUX_PREFIX/bin/stat"
readonly CONFIG_SOURCE_DEFAULT="/data/data/com.termux/files/home/.telefont.env"
readonly LOCK_FILE="$TERMUX_PREFIX/var/run/telefont-debian-worker.lock"
readonly LOG_FILE="$TERMUX_PREFIX/tmp/telefont-debian-worker.log"
readonly MAX_RESTARTS=3
readonly RESTART_WINDOW_SECONDS=300
readonly RESTART_DELAY_SECONDS=5
# T-FAST30-A23-FIX F5 hang watchdog defaults (all overridable via env below):
# the worker touches a progress beacon file on stage transitions/heartbeat
# beats; the supervisor kills the worker when the beacon is stale beyond
# WATCHDOG_STALE_MULTIPLIER x heartbeat interval. Process-lifecycle only.
readonly WATCHDOG_PROGRESS_FILE_DEFAULT="/root/.telefont_worker_progress"
readonly WATCHDOG_STALE_MULTIPLIER_DEFAULT=6
readonly WATCHDOG_POLL_SECONDS_DEFAULT=15
readonly WATCHDOG_TERM_GRACE_SECONDS=10
readonly HOST_PATH="$PATH"
readonly FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
readonly WORKER_PATH="$RUNTIME_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# T-FAST-ATLAS-ORIGINAL-GOLIVE-01 G1: runtime rebuild #1 extends the accepted
# pin set with the persistent-browser capability (playwright + greenlet + pyee,
# lock pins; versions observed from the rebuilt ARM64 runtime).
readonly EXPECTED_RUNTIME_FINGERPRINT="python=3.11.2|arch=aarch64|annotated-types=0.8.0|anyio=4.14.2|brotli=1.2.0|certifi=2026.7.22|fonttools=4.63.0|freetype-py=2.5.1|greenlet=3.5.5|h11=0.16.0|httpcore=1.0.9|httpx=0.28.1|idna=3.19|numpy=2.2.3|pillow=12.2.0|pip=26.1.2|playwright=1.62.0|pydantic=2.13.4|pydantic-core=2.46.4|pydantic-settings=2.15.0|pyee=13.0.1|python-dotenv=1.2.3|scipy=1.15.2|typing-extensions=4.16.0|typing-inspection=0.4.4|uharfbuzz=0.56.0|websockets=17.0.1"

fail_if_not_regular() {
  local path="$1"
  local label="$2"
  [ -f "$path" ] || fail "$label is unavailable"
  [ ! -L "$path" ] || fail "$label must not be a symlink"
}

verify_release_contents() {
  "$CHROOT_BIN" "$DEBIAN_ROOT" /bin/sh -s -- "$1" "$2" <<'EOF'
set -u
archive="$1"
staged="$2"
scratch="$(/usr/bin/mktemp -d)" || exit 1

cleanup() {
  /bin/rm -rf "$scratch"
}

trap cleanup EXIT HUP INT TERM

# Compare the complete non-root path set as well as content. GNU tar --compare
# ignores files that exist only in the staged tree.
/usr/bin/tar -tf "$archive" |
  /usr/bin/sed -e 's#^\./##' -e 's#/$##' |
  /usr/bin/awk 'length($0) > 0' |
  LC_ALL=C /usr/bin/sort -u >"$scratch/archive" || exit 1
/usr/bin/find "$staged" -mindepth 1 -printf '%P\n' |
  LC_ALL=C /usr/bin/sort -u >"$scratch/staged" || exit 1
LC_ALL=C /usr/bin/cmp -s "$scratch/archive" "$scratch/staged"
EOF
}

verify_archive_filesystem_identity() {
  local canonical_path="$1"
  local bridge_path="$2"
  local canonical_identity
  local bridge_identity

  [ -x "$STAT_BIN" ] || return 1
  [ -d "$canonical_path" ] && [ ! -L "$canonical_path" ] || return 1
  [ -d "$bridge_path" ] && [ ! -L "$bridge_path" ] || return 1
  canonical_identity="$("$STAT_BIN" -c '%d:%i' "$canonical_path" 2>/dev/null)" || return 1
  bridge_identity="$("$STAT_BIN" -c '%d:%i' "$bridge_path" 2>/dev/null)" || return 1
  [ -n "$canonical_identity" ] && [ "$canonical_identity" = "$bridge_identity" ]
}

clean_bytecode_caches() {
  # Worker bytecode (__pycache__) must never pollute the staged release/runtime
  # trees: the release verification compares the complete staged path set and
  # contents against the clean release archive and fails closed on any
  # worker-written .pyc. The worker also runs with PYTHONDONTWRITEBYTECODE=1.
  local target="$1"
  "$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/find "$target" -type d -name __pycache__ \
    -prune -exec /bin/rm -rf {} + >/dev/null 2>&1
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

# Hygiene before release/runtime verification: remove any worker-written
# bytecode caches so the staged trees verify against the clean release.
# Canonical ext4/mode identity checks below remain untouched.
clean_bytecode_caches "$RELEASE_ROOT" \
  || fail "release tree bytecode cache cannot be cleaned"
clean_bytecode_caches "$RUNTIME_ROOT" \
  || fail "runtime tree bytecode cache cannot be cleaned"

# The clean release archive is content-addressed and compared with the staged
# tree. A SHA-named directory alone is not a release identity.
fail_if_not_regular "$DEBIAN_ROOT$RELEASE_ARCHIVE" "release identity archive"
release_archive_hash="$("$CHROOT_BIN" "$DEBIAN_ROOT" /usr/bin/sha256sum "$RELEASE_ARCHIVE" 2>/dev/null)" \
  || fail "release identity archive cannot be hashed"
read -r release_archive_hash _ <<<"$release_archive_hash"
[ "$release_archive_hash" = "$RELEASE_ARCHIVE_SHA256" ] \
  || fail "release identity archive hash mismatch"
verify_release_contents "$RELEASE_ARCHIVE" "$RELEASE_ROOT" \
  || fail "staged release path set differs from the clean release"
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
    "greenlet", "h11", "httpcore", "httpx", "idna", "numpy", "pillow", "pip",
    "playwright", "pydantic", "pydantic-core", "pydantic-settings", "pyee",
    "python-dotenv", "scipy", "typing-extensions", "typing-inspection",
    "uharfbuzz", "websockets",
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

mkdir -p "$(dirname "$LOCK_FILE")" || fail "supervisor lock directory is unavailable"
mkdir -p "$(dirname "$LOG_FILE")" || fail "supervisor log directory is unavailable"

# D21 safe archive mode (Issue #90): explicit, versioned, never silent.
# EXTERNAL_EXT4 (default) keeps the exact canonical external ext4 filesystem
# identity checks fail-closed and unchanged. NO_LOCAL_ARCHIVE runs the worker
# without any archive root; those identity checks are skipped ONLY under that
# explicit mode and are never weakened for the canonical external path.
archive_mode="$(printenv FONT_ARCHIVE_MODE 2>/dev/null || true)"
case "$archive_mode" in
  ''|AUTO|auto) archive_mode="EXTERNAL_EXT4" ;;
  EXTERNAL_EXT4) archive_mode="EXTERNAL_EXT4" ;;
  NO_LOCAL_ARCHIVE) archive_mode="NO_LOCAL_ARCHIVE" ;;
  *) fail "unsupported FONT_ARCHIVE_MODE (expected EXTERNAL_EXT4 or NO_LOCAL_ARCHIVE)" ;;
esac

if [ "$archive_mode" = "EXTERNAL_EXT4" ]; then
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
verify_archive_filesystem_identity "$DEBIAN_ROOT$ARCHIVE_ROOT" "$HOST_ARCHIVE_BRIDGE" \
  || fail "canonical archive is not the accepted external archive filesystem"
else
  log "archive_mode=NO_LOCAL_ARCHIVE: canonical external ext4 archive identity checks skipped by explicit D21 mode"
fi

# T-FAST30-A23-FIX F5: hang watchdog configuration (config-driven; no
# A23-specific hardcoding). The heartbeat interval is read from the same
# canonical environment file the worker consumes, so the stale threshold
# tracks the worker's actual heartbeat cadence.
WATCHDOG_PROGRESS_FILE="$(printenv TELEFONT_WATCHDOG_PROGRESS_FILE 2>/dev/null || true)"
[ -n "$WATCHDOG_PROGRESS_FILE" ] || WATCHDOG_PROGRESS_FILE="$WATCHDOG_PROGRESS_FILE_DEFAULT"
case "$WATCHDOG_PROGRESS_FILE" in
  /*) : ;;
  *) fail "watchdog progress file must be an absolute chroot path" ;;
esac

WATCHDOG_STALE_MULTIPLIER="$(printenv TELEFONT_WATCHDOG_STALE_MULTIPLIER 2>/dev/null || true)"
case "$WATCHDOG_STALE_MULTIPLIER" in
  ''|*[!0-9]*) WATCHDOG_STALE_MULTIPLIER="$WATCHDOG_STALE_MULTIPLIER_DEFAULT" ;;
esac
[ "$WATCHDOG_STALE_MULTIPLIER" -ge 1 ] || fail "watchdog stale multiplier must be >= 1"

WATCHDOG_POLL_SECONDS="$(printenv TELEFONT_WATCHDOG_POLL_SECONDS 2>/dev/null || true)"
case "$WATCHDOG_POLL_SECONDS" in
  ''|*[!0-9]*) WATCHDOG_POLL_SECONDS="$WATCHDOG_POLL_SECONDS_DEFAULT" ;;
esac
[ "$WATCHDOG_POLL_SECONDS" -ge 1 ] || fail "watchdog poll seconds must be >= 1"

worker_heartbeat_interval="$(printenv HEARTBEAT_INTERVAL_SECONDS 2>/dev/null || true)"
case "$worker_heartbeat_interval" in
  ''|*[!0-9]*) worker_heartbeat_interval=60 ;;
esac
if [ "$worker_heartbeat_interval" -lt 1 ] || [ "$worker_heartbeat_interval" -gt 600 ]; then
  worker_heartbeat_interval=60
fi
WATCHDOG_STALE_SECONDS=$((WATCHDOG_STALE_MULTIPLIER * worker_heartbeat_interval))
WATCHDOG_PROGRESS_FILE_HOST="$DEBIAN_ROOT$WATCHDOG_PROGRESS_FILE"

export PATH="$HOST_PATH"

exec 9>"$LOCK_FILE" || fail "supervisor lock is unavailable"
"$FLOCK_BIN" -n 9 || fail "another Debian worker supervisor already owns the lock"

cd "$DEBIAN_ROOT" || fail "cannot enter Debian root"

log "started release=$RELEASE_SHA archive_mode=$archive_mode" || fail "supervisor log is unavailable"
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
  # Called only as a background job. The exec below replaces the backgrounded
  # subshell with the env->chroot->python chain, so the PID recorded by the
  # supervisor ($!) IS the actual worker process: stop signals reach the
  # worker itself and can never orphan it behind a bash subshell.
  if [ "$archive_mode" = "NO_LOCAL_ARCHIVE" ]; then
    # D21 NO_LOCAL_ARCHIVE: never propagate an archive root (even when one is
    # present in the host config); the worker runs the explicit versioned
    # NO_LOCAL_ARCHIVE mode - delivery works, local L1 reuse disabled.
    exec env -u FONT_ARCHIVE_ROOT \
    HOME="/root" \
    PATH="$WORKER_PATH" \
    PYTHONPATH="$RELEASE_ROOT/agent/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PROGRESS_BEACON_FILE="$WATCHDOG_PROGRESS_FILE" \
    FONT_ARCHIVE_MODE="NO_LOCAL_ARCHIVE" \
    "$CHROOT_BIN" "$DEBIAN_ROOT" "$RUNTIME_PYTHON" "$WORKER_ENTRYPOINT"
  else
    exec env \
    HOME="/root" \
    PATH="$WORKER_PATH" \
    PYTHONPATH="$RELEASE_ROOT/agent/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PROGRESS_BEACON_FILE="$WATCHDOG_PROGRESS_FILE" \
    FONT_ARCHIVE_ROOT="$ARCHIVE_ROOT" \
    FONT_ARCHIVE_MODE="EXTERNAL_EXT4" \
    "$CHROOT_BIN" "$DEBIAN_ROOT" "$RUNTIME_PYTHON" "$WORKER_ENTRYPOINT"
  fi
}

watchdog_wait_or_kill() {
  # T-FAST30-A23-FIX F5 hang watchdog: waits for worker_pid and kills it
  # when the progress beacon stays stale beyond WATCHDOG_STALE_SECONDS
  # (N x heartbeat interval). A healthy worker touches the beacon on
  # heartbeat beats during long compute and on every idle loop iteration,
  # so only a truly hung process trips this. Sets exit_code and
  # watchdog_kill; the existing restart budget then applies unchanged.
  # Process-lifecycle only: no pipeline semantics are interpreted here.
  watchdog_kill=0
  exit_code=0
  worker_start="$(date -u +%s)" || return 1
  while :; do
    if ! kill -0 "$worker_pid" 2>/dev/null; then
      wait "$worker_pid"
      exit_code=$?
      return 0
    fi
    now="$(date -u +%s)" || { wait "$worker_pid"; exit_code=$?; return 0; }
    if [ -f "$WATCHDOG_PROGRESS_FILE_HOST" ]; then
      progress_mtime="$("$STAT_BIN" -c %Y "$WATCHDOG_PROGRESS_FILE_HOST" 2>/dev/null || true)"
    else
      progress_mtime=""
    fi
    case "$progress_mtime" in
      ''|*[!0-9]*) progress_mtime="$worker_start" ;;
    esac
    stale_for=$((now - progress_mtime))
    if [ "$stale_for" -gt "$WATCHDOG_STALE_SECONDS" ]; then
      log "watchdog: progress stale ${stale_for}s (> ${WATCHDOG_STALE_SECONDS}s); killing worker pid=$worker_pid"
      watchdog_kill=1
      kill -TERM "$worker_pid" 2>/dev/null || true
      grace=0
      while kill -0 "$worker_pid" 2>/dev/null && [ "$grace" -lt "$WATCHDOG_TERM_GRACE_SECONDS" ]; do
        sleep 1
        grace=$((grace + 1))
      done
      if kill -0 "$worker_pid" 2>/dev/null; then
        kill -KILL "$worker_pid" 2>/dev/null || true
      fi
      wait "$worker_pid"
      exit_code=$?
      return 0
    fi
    sleep "$WATCHDOG_POLL_SECONDS"
  done
}

while :; do
  [ "$stop_requested" -eq 0 ] || break

  log "launching Debian worker"
  # run_worker execs the worker chain; worker_pid is the actual worker PID.
  run_worker </dev/null >>"$LOG_FILE" 2>&1 &
  worker_pid=$!
  # Incident E-00035 F2: touch progress beacon immediately after worker spawn
  # so watchdog grants a full fresh grace window to every spawned worker.
  mkdir -p "$(dirname "$WATCHDOG_PROGRESS_FILE_HOST")" 2>/dev/null || true
  : > "$WATCHDOG_PROGRESS_FILE_HOST" 2>/dev/null || touch "$WATCHDOG_PROGRESS_FILE_HOST" 2>/dev/null || true
  watchdog_wait_or_kill || fail "clock is unavailable for the hang watchdog"
  worker_pid=""
  if [ "$watchdog_kill" -eq 1 ]; then
    log "worker was killed by the hang watchdog; restart budget applies"
  fi

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
