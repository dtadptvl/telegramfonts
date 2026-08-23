"""Compatibility launcher for the canonical Debian worker supervisor."""

from pathlib import Path
import os
import select
import shutil
import subprocess
import time


STARTUP_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 2.0


def _wait_for_ready(read_fd: int) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    payload = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if not ready:
            return False
        chunk = os.read(read_fd, 128)
        if not chunk:
            return False
        payload += chunk
        if b"READY\n" in payload:
            return True
        if b"FAIL\n" in payload:
            return False


def _stop(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)


def main() -> int:
    if os.name != "posix":
        raise SystemExit("telefont-launch: POSIX supervisor boundary is required")

    supervisor = Path(__file__).with_name("debian_worker_supervisor.sh")
    if not supervisor.is_file():
        raise SystemExit("telefont-launch: Debian supervisor is unavailable")

    bash = shutil.which("bash")
    if bash is None:
        raise SystemExit("telefont-launch: bash is unavailable")

    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [bash, str(supervisor), "--ready-fd", str(write_fd)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(write_fd,),
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    else:
        os.close(write_fd)

    try:
        ready = _wait_for_ready(read_fd)
    finally:
        os.close(read_fd)

    if not ready:
        _stop(process)
        raise SystemExit("telefont-launch: supervisor preflight/lock handshake failed")

    print(f"LAUNCHED_SUPERVISOR_PID_{process.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
