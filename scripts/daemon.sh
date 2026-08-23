#!/usr/bin/env bash
# Compatibility name retained for existing Termux boot/watchdog hooks.
# The worker itself is launched only by the canonical Debian boundary.

set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || {
  printf 'telefont-daemon: cannot resolve supervisor directory\n' >&2
  exit 1
}

exec "${BASH}" "${SCRIPT_DIR}/debian_worker_supervisor.sh" "$@"
