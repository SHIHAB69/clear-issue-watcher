"""Cross-platform background scheduling for the watcher's periodic run.
One abstraction, three backends chosen by OS:
  macOS   → launchd user agent
  Linux   → systemd user timer (fallback: cron)
  Windows → Task Scheduler (schtasks)
`watcher start` installs it; `watcher stop` removes it; `watcher status` reports.
The scheduled command is: `<python> -m watcher.run_cycle` every INTERVAL seconds.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

LABEL = "com.watcher.runner"
DEFAULT_INTERVAL = 120


def _python() -> str:
    return sys.executable or "python3"


def _run_cmd() -> list[str]:
    return [_python(), "-m", "watcher.run_cycle"]


def _os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "nt":
        return "windows"
    return "unknown"


# ---------------- macOS (launchd) ----------------
def _launchd_plist() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _macos_start(interval: int, env: dict):
    p = _launchd_plist()
    p.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": _run_cmd(),
        "StartInterval": interval,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", ""), **env},
        "StandardOutPath": str(config.HOME / "runner.out"),
        "StandardErrorPath": str(config.HOME / "runner.err"),
    }
    with p.open("wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    subprocess.run(["launchctl", "load", str(p)], capture_output=True)
    return f"launchd agent installed ({p}), every {interval}s"


def _macos_stop():
    p = _launchd_plist()
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    p.unlink(missing_ok=True)
    return "launchd agent removed"


def _macos_status():
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return "running" if LABEL in r.stdout else "not loaded"


# ---------------- Linux (systemd user) ----------------
def _systemd_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def _linux_start(interval: int, env: dict):
    if not shutil.which("systemctl"):
        return _cron_start(interval, env)
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    envlines = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())
    (d / "watcher.service").write_text(
        f"[Unit]\nDescription=Watcher runner\n[Service]\nType=oneshot\n"
        f"ExecStart={' '.join(_run_cmd())}\n{envlines}\n")
    (d / "watcher.timer").write_text(
        f"[Unit]\nDescription=Watcher runner timer\n[Timer]\n"
        f"OnBootSec={interval}\nOnUnitActiveSec={interval}\n"
        f"[Install]\nWantedBy=timers.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "watcher.timer"], capture_output=True)
    return f"systemd user timer installed, every {interval}s"


def _linux_stop():
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "watcher.timer"], capture_output=True)
        for f in ("watcher.timer", "watcher.service"):
            (_systemd_dir() / f).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "systemd user timer removed"
    return _cron_stop()


def _linux_status():
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-active", "watcher.timer"],
                           capture_output=True, text=True)
        return r.stdout.strip() or "inactive"
    return _cron_status()


# ---------------- cron fallback ----------------
_CRON_MARK = "# watcher-runner"


def _cron_start(interval: int, env: dict | None = None):
    # cron granularity is 1 min; run every minute, the per-source lock dedupes.
    path = (env or {}).get("PATH", "")
    prefix = f'PATH="{path}" ' if path else ""     # cron has a minimal PATH by default
    line = f"* * * * * {prefix}{' '.join(_run_cmd())}  {_CRON_MARK}"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    cur = "\n".join(l for l in cur.splitlines() if _CRON_MARK not in l)
    subprocess.run(["crontab", "-"], input=cur + "\n" + line + "\n", text=True)
    return "cron entry installed (every 1 min)"


def _cron_stop():
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    cur = "\n".join(l for l in cur.splitlines() if _CRON_MARK not in l)
    subprocess.run(["crontab", "-"], input=cur + "\n", text=True)
    return "cron entry removed"


def _cron_status():
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    return "running" if _CRON_MARK in cur else "not installed"


# ---------------- Windows (schtasks) ----------------
def _windows_start(interval: int, env: dict):
    cmd = " ".join([f'"{_python()}"', "-m", "watcher.run_cycle"])
    mins = max(1, interval // 60)
    subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(mins),
                    "/TN", "WatcherRunner", "/TR", cmd], capture_output=True)
    return f"Task Scheduler job installed, every {mins} min"


def _windows_stop():
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", "WatcherRunner"], capture_output=True)
    return "Task Scheduler job removed"


def _windows_status():
    r = subprocess.run(["schtasks", "/Query", "/TN", "WatcherRunner"], capture_output=True, text=True)
    return "running" if r.returncode == 0 else "not installed"


# ---------------- dispatch ----------------
def start(interval: int = DEFAULT_INTERVAL, env: dict | None = None) -> str:
    # capture the installer's PATH (nvm/npm dirs for claude/gh) for ALL backends,
    # not just macOS — otherwise scheduled Linux/Windows runs can't find claude/gh.
    env = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    return {"macos": _macos_start, "linux": _linux_start, "windows": _windows_start}\
        .get(_os(), lambda *_: "unsupported OS")(interval, env)


def stop() -> str:
    return {"macos": _macos_stop, "linux": _linux_stop, "windows": _windows_stop}\
        .get(_os(), lambda: "unsupported OS")()


def status() -> str:
    return {"macos": _macos_status, "linux": _linux_status, "windows": _windows_status}\
        .get(_os(), lambda: "unsupported OS")()
