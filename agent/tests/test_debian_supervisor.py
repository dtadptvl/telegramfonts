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


def test_supervisor_worker_launch_disables_bytecode_and_execs_worker() -> None:
    source = _text(SUPERVISOR)

    start = source.index("run_worker() {")
    end = source.index("\n}\n", start) + 2
    body = source[start:end]

    assert body.count("PYTHONDONTWRITEBYTECODE=1") == 2
    assert body.count("exec env") == 2
    assert 'exec env -u FONT_ARCHIVE_ROOT' in body
    assert 'run_worker </dev/null >>"$LOG_FILE" 2>&1 &' in source


def test_supervisor_cleans_bytecode_caches_before_release_verification() -> None:
    source = _text(SUPERVISOR)

    function = _shell_function(source, "clean_bytecode_caches")
    assert '/usr/bin/find "$target" -type d -name __pycache__' in function
    assert '-exec /bin/rm -rf {} +' in function

    # Hygiene runs before release verification; the canonical ext4/mode
    # identity checks remain present and untouched.
    assert source.index('clean_bytecode_caches "$RELEASE_ROOT"') < source.index(
        'verify_release_contents "$RELEASE_ARCHIVE" "$RELEASE_ROOT"'
    )
    assert source.index('clean_bytecode_caches "$RUNTIME_ROOT"') < source.index(
        'verify_release_contents "$RELEASE_ARCHIVE" "$RELEASE_ROOT"'
    )
    assert 'release tree bytecode cache cannot be cleaned' in source
    assert 'runtime tree bytecode cache cannot be cleaned' in source
    assert 'verify_archive_filesystem_identity "$DEBIAN_ROOT$ARCHIVE_ROOT" "$HOST_ARCHIVE_BRIDGE"' in source


def _stop_probe_script(run_worker_source: str, temp_dir: str) -> str:
    # Causal stop-semantics probe for the supervisor's recorded worker PID.
    # The extracted run_worker runs against a fake chroot whose process
    # records its own PID. The asserted causal property: the PID the
    # supervisor records ($!) IS the actual worker process PID, so the
    # supervisor's TERM reaches the worker itself and can never be absorbed
    # by an intervening bash subshell (the observed device failure mode).
    # The explicit `exec` in run_worker guarantees this PID identity
    # deterministically; without it the identity depends on bash-version
    # exec-optimization heuristics and was observed to break on device.
    return "\n".join(
        (
            "set -u",
            f'cd "{temp_dir}" || {{ echo CD_FAILED; exit 1; }}',
            'archive_mode="NO_LOCAL_ARCHIVE"',
            'CHROOT_BIN="$PWD/fake_chroot.sh"',
            'DEBIAN_ROOT="/nonexistent"',
            'RELEASE_ROOT="/nonexistent"',
            'RUNTIME_PYTHON="/nonexistent"',
            'WORKER_ENTRYPOINT="/nonexistent"',
            'ARCHIVE_ROOT="/nonexistent"',
            'WATCHDOG_PROGRESS_FILE="/root/.telefont_worker_progress"',
            'WORKER_PATH="/usr/bin:/bin"',
            'export pidfile="$PWD/sentinel.pid"',
            "cat >fake_chroot.sh <<'STUB'",
            "#!/bin/sh",
            'printf "%s\\n" "$$" >"$pidfile"',
            "exec sleep 30 # telefont-stop-probe-sentinel",
            "STUB",
            "chmod +x fake_chroot.sh",
            run_worker_source,
            'run_worker </dev/null >/dev/null 2>&1 &',
            "worker_pid=$!",
            "sleep 1",
            'sentinel_pid="$(cat "$pidfile" 2>/dev/null || true)"',
            '[ -n "$sentinel_pid" ] || { echo NO_SENTINEL_PID; exit 1; }',
            '[ "$worker_pid" = "$sentinel_pid" ] || { echo PID_MISMATCH "$worker_pid" "$sentinel_pid"; exit 1; }',
            'kill -TERM "$worker_pid" 2>/dev/null || { echo TERM_FAILED; exit 1; }',
            'wait "$worker_pid" 2>/dev/null || true',
            "sleep 1",
            'if kill -0 "$sentinel_pid" 2>/dev/null; then',
            '  kill -KILL "$sentinel_pid" 2>/dev/null || true',
            "  echo ORPHANED",
            "  exit 1",
            "fi",
            "echo STOPPED",
        )
    )


def test_supervisor_stop_terminates_actual_worker_process() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    run_worker_source = _shell_function(source, "run_worker")

    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        result = subprocess.run(
            [bash, "-c", _stop_probe_script(run_worker_source, Path(temp_dir).as_posix())],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "STOPPED" in result.stdout

def test_supervisor_bytecode_cache_clean_removes_only_pycache() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    function = _shell_function(source, "clean_bytecode_caches")

    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        temp_root = Path(temp_dir)
        staged = temp_root / "staged"
        (staged / "agent" / "src" / "__pycache__").mkdir(parents=True)
        (staged / "agent" / "__pycache__").mkdir(parents=True)
        (staged / "agent" / "src" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"x")
        (staged / "agent" / "src" / "main.py").write_text("X = 1\n", encoding="utf-8")
        fake_chroot = temp_root / "fake_chroot.sh"
        fake_chroot.write_text(
            "\n".join(
                (
                    "#!/bin/sh",
                    "shift",
                    'cmd="$1"',
                    "shift",
                    'case "$cmd" in',
                    '  /usr/bin/find) exec find "$@" ;;',
                    '  /bin/rm) exec rm "$@" ;;',
                    "esac",
                    "",
                )
            ),
            encoding="utf-8",
        )
        fake_chroot.chmod(0o755)
        script = "\n".join(
            (
                "set -u",
                'CHROOT_BIN="$PWD/fake_chroot.sh"',
                'DEBIAN_ROOT="/nonexistent"',
                function,
                'clean_bytecode_caches "$PWD/staged"',
            )
        )
        result = subprocess.run(
            [bash, "-c", script],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not list(staged.rglob("__pycache__"))
        assert (staged / "agent" / "src" / "main.py").read_text(encoding="utf-8") == "X = 1\n"


def test_supervisor_watchdog_is_config_driven_and_exports_beacon() -> None:
    source = _text(SUPERVISOR)

    # Defaults are documented and overridable (config-driven; no hardcoding).
    assert 'readonly WATCHDOG_PROGRESS_FILE_DEFAULT="/root/.telefont_worker_progress"' in source
    assert 'readonly WATCHDOG_STALE_MULTIPLIER_DEFAULT=6' in source
    assert 'readonly WATCHDOG_POLL_SECONDS_DEFAULT=15' in source
    assert 'printenv TELEFONT_WATCHDOG_PROGRESS_FILE' in source
    assert 'printenv TELEFONT_WATCHDOG_STALE_MULTIPLIER' in source
    assert 'printenv TELEFONT_WATCHDOG_POLL_SECONDS' in source
    # Stale threshold tracks the worker's configured heartbeat cadence.
    assert 'printenv HEARTBEAT_INTERVAL_SECONDS' in source
    assert 'WATCHDOG_STALE_SECONDS=$((WATCHDOG_STALE_MULTIPLIER * worker_heartbeat_interval))' in source
    # The worker receives the beacon path (chroot coordinates) in both chains.
    assert source.count('PROGRESS_BEACON_FILE="$WATCHDOG_PROGRESS_FILE"') == 2
    # Host-side watchdog reads the beacon through the Debian root.
    assert 'WATCHDOG_PROGRESS_FILE_HOST="$DEBIAN_ROOT$WATCHDOG_PROGRESS_FILE"' in source
    # Lifecycle controls preserved: restart budget still applies after a kill.
    assert 'watchdog_wait_or_kill || fail' in source
    assert 'worker was killed by the hang watchdog; restart budget applies' in source
    assert 'readonly MAX_RESTARTS=3' in source

    # Incident E-00035 F2: watchdog beacon is touched on worker spawn
    assert ': > "$WATCHDOG_PROGRESS_FILE_HOST"' in source or 'touch "$WATCHDOG_PROGRESS_FILE_HOST"' in source


def _watchdog_probe(function_source: str, temp_dir: str, scenario: str) -> str:
    # Causal watchdog probe: the extracted watchdog_wait_or_kill runs against
    # a fake worker. "stale" scenario: the worker never touches the beacon
    # and must be killed after the stale threshold. "fresh" scenario: the
    # beacon is fresh and the worker exits naturally; the watchdog must NOT
    # kill (process-lifecycle only, no false positives).
    beacon_touch = "touch \"$WATCHDOG_PROGRESS_FILE_HOST\"\n" if scenario == "fresh" else ""
    worker_cmd = "sleep 1" if scenario == "fresh" else "sleep 30"
    return "\n".join(
        (
            "set -u",
            f'cd "{temp_dir}" || exit 1',
            'DEBIAN_ROOT="$PWD/rootfs"',
            'mkdir -p "$DEBIAN_ROOT/root"',
            'WATCHDOG_PROGRESS_FILE_HOST="$DEBIAN_ROOT/root/.telefont_worker_progress"',
            "WATCHDOG_STALE_SECONDS=2",
            "WATCHDOG_POLL_SECONDS=1",
            "WATCHDOG_TERM_GRACE_SECONDS=2",
            'STAT_BIN="$(command -v stat)"',
            'LOG_FILE="$PWD/watchdog.log"',
            'log() { printf \'%s\\n\' "$1" >>"$LOG_FILE"; }',
            function_source,
            beacon_touch + f'{worker_cmd} </dev/null >/dev/null 2>&1 &',
            "worker_pid=$!",
            'watchdog_wait_or_kill || { echo CLOCK_FAIL; exit 1; }',
            'echo "watchdog_kill=$watchdog_kill"',
            'if kill -0 "$worker_pid" 2>/dev/null; then',
            '  kill -KILL "$worker_pid" 2>/dev/null || true',
            "  echo ORPHAN",
            "else",
            "  echo WORKER_DEAD",
            "fi",
        )
    )


def test_supervisor_watchdog_kills_worker_with_stale_progress() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    function = _shell_function(source, "watchdog_wait_or_kill")

    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        result = subprocess.run(
            [bash, "-c", _watchdog_probe(function, Path(temp_dir).as_posix(), "stale")],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "watchdog_kill=1" in result.stdout
        assert "WORKER_DEAD" in result.stdout


def test_supervisor_watchdog_does_not_kill_worker_with_fresh_progress() -> None:
    bash = _bash_command()
    if bash is None:
        pytest.skip("bash is not available on the validation host")

    source = _text(SUPERVISOR)
    function = _shell_function(source, "watchdog_wait_or_kill")

    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        result = subprocess.run(
            [bash, "-c", _watchdog_probe(function, Path(temp_dir).as_posix(), "fresh")],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "watchdog_kill=0" in result.stdout
        assert "WORKER_DEAD" in result.stdout


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
