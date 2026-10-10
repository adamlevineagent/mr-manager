"""`mrm setup`: everything needed, then a real worker as proof.

1. checks: Python, git, the Codex CLI, sign-in and a real API turn for every account
2. puts `mrm` on your PATH
3. installs the Claude Code skill (so Claude Code knows how to manage workers)
4. runs the daemon as a supervised background service (restarts on exit, starts at sign-in)
5. smoke test: assigns a tiny task in a scratch repo, waits, accepts it, and shows the merge
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core, probe

ROOT = Path(__file__).resolve().parent.parent


def say(ok, msg):
    print(("  ok   " if ok else "  !!   ") + msg)
    return ok


def run(a):
    print("mr-manager setup\n")
    ok = say(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    ok &= say(bool(shutil.which("git")), "git" + ("" if shutil.which("git") else " is missing: install git first"))
    from .appserver import find_codex
    exe = find_codex()
    try:
        say(True, f"Codex CLI: {exe} ({probe.cli_version(exe)})")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        ok = False
        say(False, f"Codex CLI: {exe}: {error}")
        print("       hint: install Codex or set MRM_CODEX to another codex")
    if not ok:
        raise SystemExit("\nfix the items marked !! and run mrm setup again")

    cfg = core.config()
    for name, acc in cfg["accounts"].items():
        home = Path(acc["home"])
        signed = (home / "auth.json").exists()
        if not say(signed, f"account {name}: {home}" + ("" if signed else " is not signed in")):
            print(f"       signing in account {name} (a browser window opens)...")
            subprocess.call([exe, "login"], env=dict(os.environ, CODEX_HOME=str(home)))
        if not probe.probe_account(name, acc, cfg, exe):
            raise SystemExit(1)
    core.save_config(cfg)

    say(True, "mrm on PATH: " + install_launcher())
    say(True, "Claude Code skill: " + install_skill())
    say(True, "daemon service: " + install_service())
    # The service starts the daemon itself; give it time before starting one by hand, or two daemons race for
    # the port and the service's copy crash-loops on "address already in use" (seen on macOS).
    for _ in range(40):
        if core.daemon_alive():
            break
        time.sleep(0.5)
    if not core.daemon_alive():
        core.ensure_daemon()
    say(bool(core.daemon_alive()), f"daemon answering on 127.0.0.1:{core.PORT}")

    if getattr(a, "skip_smoke", False):
        print("\nskipped the smoke test. You're set: mrm status")
        return
    smoke()


def stable_python():
    """The interpreter path to bake into launchers and services. Homebrew's sys.executable is a versioned
    Cellar path that `brew upgrade` deletes; prefer a stable symlink that resolves to the same interpreter."""
    exe = sys.executable
    if os.name == "nt":
        return exe
    real = os.path.realpath(exe)
    for name in (f"python{sys.version_info.major}.{sys.version_info.minor}", f"python{sys.version_info.major}"):
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            c = os.path.join(d, name)
            if os.path.exists(c) and os.path.realpath(c) == real:
                return c
    return exe


def service_path():
    """PATH for a background service: services start with /usr/bin:/bin only, which has no codex, and on
    macOS no Homebrew git or python."""
    dirs = [str(Path(stable_python()).parent), "/opt/homebrew/bin", "/usr/local/bin",
            str(Path.home() / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    from .appserver import find_codex
    exe = find_codex()
    if os.path.isabs(exe):
        dirs.insert(0, str(Path(exe).parent))
    seen = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def install_launcher():
    py = stable_python()
    if os.name == "nt":
        target_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"   # on PATH by default
        target = target_dir / "mrm.cmd"
        target.write_text(f'@echo off\r\n"{py}" -X utf8 "{ROOT / "mrm.py"}" %*\r\n', encoding="utf-8")
    else:
        target_dir = Path.home() / ".local" / "bin"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "mrm"
        target.write_text(f'#!/bin/sh\nexec "{py}" "{ROOT / "mrm.py"}" "$@"\n', encoding="utf-8")
        target.chmod(0o755)
        if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            return (f"{target} (not on your PATH yet: add  export PATH=\"$HOME/.local/bin:$PATH\"  to "
                    f"~/.zshrc, then open a new terminal)")
    return str(target)


def install_skill():
    dst = Path.home() / ".claude" / "skills" / "mr-manager"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-skill" / "SKILL.md", dst / "SKILL.md")
    return str(dst / "SKILL.md")


def install_service():
    sup = ROOT / "mrm_supervisor.py"
    if os.name == "nt":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        ps = f"""
$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '-X utf8 "{sup}"' -WorkingDirectory '{ROOT}'
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName 'Mr Manager daemon' -Description 'mr-manager daemon (supervised)' -Action $a -Trigger $t -Settings $s -Force | Out-Null
if (Get-ScheduledTask -TaskName 'Codex Foreman daemon' -ErrorAction SilentlyContinue) {{ Unregister-ScheduledTask -TaskName 'Codex Foreman daemon' -Confirm:$false }}
Start-ScheduledTask -TaskName 'Mr Manager daemon'
"""
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        return "scheduled task 'Mr Manager daemon' (starts at sign-in, restarts on exit)"
    if sys.platform == "darwin":
        label = "dev.mr-manager.daemon"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        core.HOME.mkdir(parents=True, exist_ok=True)
        log = core.HOME / "daemon.log"
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array><string>{stable_python()}</string><string>-X</string><string>utf8</string><string>{ROOT / 'mrm.py'}</string><string>daemon</string></array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{service_path()}</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>""", encoding="utf-8")
        # `launchctl load` is deprecated and silently keeps an already-loaded old definition; replace it.
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "kickstart", f"{domain}/{label}"], capture_output=True)
        return f"launchd agent {plist} (starts at sign-in, restarts on exit, log {log})"
    unit = Path.home() / ".config" / "systemd" / "user" / "mr-manager.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"[Unit]\nDescription=mr-manager daemon\n[Service]\nExecStart={sys.executable} {ROOT / 'mrm.py'} daemon\n"
                    "Restart=always\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "enable", "--now", "mr-manager.service"], capture_output=True)
    return f"systemd user service {unit}"


def smoke():
    from . import cli
    print("\nsmoke test: a real worker does a tiny task in a scratch repo, then I accept it")
    repo = Path(tempfile.mkdtemp(prefix="mrm-smoke-"))
    g = lambda *args: subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.name", "mrm smoke")
    g("config", "user.email", "smoke@example.invalid")
    (repo / "README.md").write_text("smoke test repo\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    name = f"smoke-{int(time.time()) % 100000}"
    check = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'hello.txt\').read_text().strip()==\'hello from mrm\' else 1)"'
    t0 = time.time()
    cli.main(["assign", name, "A file hello.txt at the repo root containing exactly: hello from mrm",
              "--repo", str(repo), "--check", check, "--size", "small", "--timebox", "5"])
    cli.main(["wait", name, "--timeout", "900"])
    cli.main(["accept", name, "--commit"])
    merged = (repo / "hello.txt").exists()
    print(f"\n{'PASSED' if merged else 'FAILED'}: worker briefed, checked and merged in {int(time.time() - t0)}s. "
          f"You're set. Try: mrm status")
    shutil.rmtree(repo, ignore_errors=True)
