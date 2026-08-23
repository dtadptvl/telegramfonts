"""Static contract tests for the D12 Debian worker boundary."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[2]
SUPERVISOR = ROOT / "scripts" / "debian_worker_supervisor.sh"
DAEMON = ROOT / "scripts" / "daemon.sh"
LAUNCH = ROOT / "scripts" / "launch.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_supervisor_selects_explicit_debian_release_and_runtime() -> None:
    source = _text(SUPERVISOR)

    assert 'RELEASE_SHA="582dba833bf4f955e872823b99ee24a57fde21b3"' in source
    assert 'DEBIAN_ROOT="/data/local/chroot/debian"' in source
    assert 'CHROOT_BIN="/data/data/com.termux/files/usr/bin/chroot"' in source
    assert 'RELEASE_ROOT="/opt/telefont-release-${RELEASE_SHA}"' in source
    assert 'RUNTIME_ROOT="/opt/telefont-runtime-${RELEASE_SHA}"' in source
    assert '"${CHROOT_BIN}" "${DEBIAN_ROOT}" "${RUNTIME_PYTHON}" "${WORKER_ENTRYPOINT}"' in source
    assert 'PYTHONPATH="${RELEASE_ROOT}/agent/src"' in source


def test_supervisor_fails_closed_and_propagates_canonical_archive_config() -> None:
    source = _text(SUPERVISOR)

    for required in (
        '[ -d "${DEBIAN_ROOT}" ] || fail',
        '[ -x "${CHROOT_BIN}" ] || fail',
        '[ -d "${DEBIAN_ROOT}${RELEASE_ROOT}" ] || fail',
        '[ -f "${DEBIAN_ROOT}${WORKER_ENTRYPOINT}" ] || fail',
        '[ -d "${DEBIAN_ROOT}${RUNTIME_ROOT}" ] || fail',
        '"${CHROOT_BIN}" "${DEBIAN_ROOT}" /usr/bin/test -x "${RUNTIME_PYTHON}"',
        '[ -d "${DEBIAN_ROOT}${ARCHIVE_ROOT}" ] || fail',
        '[ -n "${FLOCK_BIN}" ] || fail',
        '[ -f "${CONFIG_SOURCE}" ] || fail',
        '"${FLOCK_BIN}" -n 9',
    ):
        assert required in source

    assert 'FONT_ARCHIVE_ROOT="${ARCHIVE_ROOT}"' in source
    assert 'readonly ARCHIVE_ROOT="/srv/fontlab/archive"' in source
    assert 'HOME="/root"' in source
    assert 'readonly MAX_RESTARTS=3' in source
    assert 'readonly RESTART_WINDOW_SECONDS=300' in source
    assert 'readonly RESTART_DELAY_SECONDS=5' in source
    assert 'trap forward_stop INT TERM HUP' in source


def test_no_legacy_termux_worker_fallback_remains() -> None:
    sources = [_text(SUPERVISOR), _text(DAEMON), _text(LAUNCH)]
    combined = "\n".join(sources)

    for forbidden in (
        '${HOME}/telefont',
        'python "${ROOT_DIR}',
        '/data/data/com.termux/files/home/telefont/agent/src/main.py',
        'sys.executable',
    ):
        assert forbidden not in combined

    assert 'debian_worker_supervisor.sh' in _text(DAEMON)
    assert 'debian_worker_supervisor.sh' in _text(LAUNCH)


def test_supervisor_shell_syntax() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    result = subprocess.run(
        [bash, "-n", "scripts/debian_worker_supervisor.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
