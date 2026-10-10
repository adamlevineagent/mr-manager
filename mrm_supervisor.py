"""Keeps the mr-manager daemon running; started by the "Mr Manager daemon" scheduled task
(or launchd/systemd) so it never depends on an agent or terminal session. If another daemon already
holds the port, this one keeps retrying with backoff and takes over when that one exits."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from foreman.core import HOME  # noqa: E402
python = Path(sys.executable).with_name("python.exe" if sys.platform == "win32" else Path(sys.executable).name.replace("pythonw", "python"))


def log(msg):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "supervisor.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


fails = 0
while True:
    started = time.time()
    with open(HOME / "daemon.log", "a", encoding="utf-8") as out:
        code = subprocess.call([str(python), "-X", "utf8", str(ROOT / "mrm.py"), "daemon"], cwd=ROOT,
                               stdout=out, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ran = time.time() - started
    fails = fails + 1 if ran < 30 else 0
    if fails <= 1 or fails % 10 == 0:
        log(f"daemon exited with code {code} after {ran:.0f}s")
    time.sleep(min(120, 5 * 2 ** min(fails, 5)))
