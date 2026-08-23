"""Compatibility launcher for the canonical Debian worker supervisor."""

from pathlib import Path
import shutil
import subprocess


supervisor = Path(__file__).with_name("debian_worker_supervisor.sh")
if not supervisor.is_file():
    raise SystemExit("telefont-launch: Debian supervisor is unavailable")

bash = shutil.which("bash")
if bash is None:
    raise SystemExit("telefont-launch: bash is unavailable")

p = subprocess.Popen(
    [bash, str(supervisor)],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    close_fds=True,
)
print(f"LAUNCHED_SUPERVISOR_PID_{p.pid}")
