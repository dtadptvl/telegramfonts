"""Static and bounded handshake tests for the D12 Debian worker boundary."""

from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import tarfile
import tempfile

import pytest


ROOT = Path(__file__).parents[2]
SUPERVISOR = ROOT / "scripts" / "debian_worker_supervisor.sh"
DAEMON = ROOT / "scripts" / "daemon.sh"
LAUNCH = ROOT / "scripts" / "launch.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bash_command() -> str | None:
    if os.name == "nt":
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def _launch_module():
    spec = importlib.util.spec_from_file_location("telefont_launch", LAUNCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _embedded_shell_probe(source: str) -> str:
    start = source.index('set -u\narchive="$1"\nstaged="$2"')
    end = source.index("\nEOF", start)
    return source[start:end]


def _shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 2
    return source[start:end]


def test_supervisor_selects_explicit_debian_release_and_runtime() -> None:
    source = _text(SUPERVISOR)

    assert 'RELEASE_SHA="582dba833bf4f955e872823b99ee24a57fde21b3"' in source
    assert 'DEBIAN_ROOT="/data/local/chroot/debian"' in source
    assert 'CHROOT_BIN="/data/data/com.termux/files/usr/bin/chroot"' in source
    assert 'RELEASE_ROOT="/opt/telefont-release-$RELEASE_SHA"' in source
    assert 'RUNTIME_ROOT="/opt/telefont-runtime-$RELEASE_SHA"' in source
    assert '"$CHROOT_BIN" "$DEBIAN_ROOT" "$RUNTIME_PYTHON" "$WORKER_ENTRYPOINT"' in source
    assert 'PYTHONPATH="$RELEASE_ROOT/agent/src"' in source


def test_supervisor_verifies_release_and_runtime_identity() -> None:
    source = _text(SUPERVISOR)

    assert 'RELEASE_ARCHIVE="/opt/telefont-release-$RELEASE_SHA.tar"' in source
    assert 'RELEASE_ARCHIVE_SHA256="02363eac3ea3f611bcbd94608c15379fa2b0f6c0d4286c244f83ba9122ef24ab"' in source
    assert '/usr/bin/sha256sum "$RELEASE_ARCHIVE"' in source
    assert '/usr/bin/tar -d -f "$RELEASE_ARCHIVE" -C "$RELEASE_ROOT"' in source
    assert 'RUNTIME_MANIFEST="/opt/telefont-runtime-$RELEASE_SHA.manifest"' in source
    assert 'telefont-runtime-fingerprint=' in source
    assert '/usr/bin/sha256sum -c "$manifest"' in source
    assert 'EXPECTED_RUNTIME_FINGERPRINT=' in source
    assert 'importlib.metadata' in source
    assert 'metadata.distributions()' in source
    assert 'platform.machine().lower()' in source
    assert 'runtime_fingerprint=' in source


def test_release_identity_rejects_extra_staged_path() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    probe = _embedded_shell_probe(source)
    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        temp_root = Path(temp_dir)
        relative_root = temp_root.relative_to(ROOT).as_posix()
        archive = temp_root / "release.tar"
        staged = temp_root / "staged"
        entrypoint = staged / "agent" / "src" / "main.py"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("print('clean')\n", encoding="utf-8")
        with tarfile.open(archive, "w") as release:
            release.add(staged, arcname=".")

        probe_with_paths = probe.replace(
            'archive="$1"', f'archive="{relative_root}/release.tar"'
        ).replace(
            'staged="$2"', f'staged="{relative_root}/staged"'
        )

        def run_probe() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [bash, "-c", probe_with_paths],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        assert run_probe().returncode == 0
        (staged / "agent" / "src" / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
        assert run_probe().returncode != 0


def test_archive_identity_rejects_unaccepted_filesystem() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    function = _shell_function(source, "verify_archive_filesystem_identity")
    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        temp_root = Path(temp_dir)
        relative_root = temp_root.relative_to(ROOT).as_posix()
        (temp_root / "canonical").mkdir()
        (temp_root / "wrong-filesystem").mkdir()
        script = "\n".join(
            (
                "set -u",
                'STAT_BIN="$(command -v stat)"',
                function,
                f'verify_archive_filesystem_identity "{relative_root}/canonical" "{relative_root}/wrong-filesystem"',
            )
        )
        result = subprocess.run(
            [bash, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0


def test_supervisor_requires_external_canonical_archive_mount() -> None:
    source = _text(SUPERVISOR)

    assert '/proc/self/mountinfo' in source
    assert '$(i + 1) == "ext4"' in source
    assert '/usr/bin/stat -c %d "$ARCHIVE_ROOT"' in source
    assert '/usr/bin/stat -c %d /' in source
    assert 'archive_device' in source
    assert 'root_device' in source
    assert 'canonical archive resolves to the Debian root device' in source
    assert 'HOST_ARCHIVE_BRIDGE="/data/data/com.termux/files/home/telefont-archive-bridge"' in source
    assert 'STAT_BIN="$TERMUX_PREFIX/bin/stat"' in source
    assert "-c '%d:%i'" in source
    assert 'canonical archive is not the accepted external archive filesystem' in source


def test_supervisor_fails_closed_and_preserves_runtime_controls() -> None:
    source = _text(SUPERVISOR)

    for required in (
        '[ -d "$DEBIAN_ROOT" ] || fail',
        '[ -x "$CHROOT_BIN" ] || fail',
        '[ -d "$DEBIAN_ROOT$RELEASE_ROOT" ] || fail',
        '[ -f "$DEBIAN_ROOT$WORKER_ENTRYPOINT" ] || fail',
        '[ -d "$DEBIAN_ROOT$RUNTIME_ROOT" ] || fail',
        '[ -n "$FLOCK_BIN" ] || fail',
        '[ -f "$CONFIG_SOURCE" ] || fail',
        '"$FLOCK_BIN" -n 9',
    ):
        assert required in source

    assert 'FONT_ARCHIVE_ROOT="$ARCHIVE_ROOT"' in source
    assert 'readonly ARCHIVE_ROOT="/srv/fontlab/archive"' in source
    assert 'HOME="/root"' in source
    assert 'readonly MAX_RESTARTS=3' in source
    assert 'readonly RESTART_WINDOW_SECONDS=300' in source
    assert 'readonly RESTART_DELAY_SECONDS=5' in source
    assert 'trap forward_stop INT TERM HUP' in source


def test_launch_waits_for_supervisor_handshake_before_reporting_pid() -> None:
    source = _text(LAUNCH)
    supervisor = _text(SUPERVISOR)

    assert '--ready-fd' in source
    assert 'pass_fds=(write_fd,)' in source
    assert 'select.select' in source
    assert 'STARTUP_TIMEOUT_SECONDS' in source
    assert 'supervisor preflight/lock handshake failed' in source
    assert source.index('ready = _wait_for_ready') < source.index('LAUNCHED_SUPERVISOR_PID_')
    assert 'READY' in supervisor
    assert 'FAIL' in supervisor


def test_launch_handshake_distinguishes_ready_and_failure() -> None:
    if os.name != "posix":
        pytest.skip("POSIX pipe handshake is only used on Termux")

    module = _launch_module()

    ready_read, ready_write = os.pipe()
    try:
        os.write(ready_write, b'READY\n')
        assert module._wait_for_ready(ready_read) is True
    finally:
        os.close(ready_read)
        os.close(ready_write)

    fail_read, fail_write = os.pipe()
    try:
        os.write(fail_write, b'FAIL\n')
        assert module._wait_for_ready(fail_read) is False
    finally:
        os.close(fail_read)
        os.close(fail_write)


def test_no_legacy_termux_worker_fallback_remains() -> None:
    sources = [_text(SUPERVISOR), _text(DAEMON), _text(LAUNCH)]
    combined = "\n".join(sources)

    for forbidden in (
        "$" + "{HOME}/telefont",
        'python "$ROOT_DIR',
        '/data/data/com.termux/files/home/telefont/agent/src/main.py',
        'sys.executable',
    ):
        assert forbidden not in combined

    assert 'debian_worker_supervisor.sh' in _text(DAEMON)
    assert 'debian_worker_supervisor.sh' in _text(LAUNCH)


def test_supervisor_shell_syntax() -> None:
    bash = _bash_command()
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
