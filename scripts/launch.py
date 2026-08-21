import os
import subprocess
import sys

env = os.environ.copy()
env["PYTHONPATH"] = "/data/data/com.termux/files/home/telefont/agent/src"

log_path = "/data/data/com.termux/files/usr/tmp/telefont.log" if os.path.exists("/data/data/com.termux/files/usr/tmp") else "/tmp/telefont.log"
os.makedirs(os.path.dirname(log_path), exist_ok=True)

with open(log_path, "a") as log:
    p = subprocess.Popen(
        [sys.executable, "/data/data/com.termux/files/home/telefont/agent/src/main.py"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        close_fds=True,
    )
print(f"LAUNCHED_PID_{p.pid}")
