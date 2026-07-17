"""Entry point the OS scheduler invokes each interval: run one cycle over all
sources, headless (non-interactive → autonomous). A global lock prevents two
cycles overlapping if one runs long."""
import os
import time

from . import config, engine

_GLOBAL_LOCK = config.HOME / "cycle.lock"


def _lock_is_live() -> bool:
    """True if a previous cycle is genuinely still running. Uses the recorded PID
    so a crashed/killed cycle is reclaimed immediately, with a 1h mtime backstop
    against PID reuse after a reboot."""
    try:
        raw = _GLOBAL_LOCK.read_text().strip()
        if time.time() - _GLOBAL_LOCK.stat().st_mtime >= 3600:
            return False                # too old → stale regardless (PID reuse guard)
    except (FileNotFoundError, OSError):
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False                    # unparseable → treat as stale
    try:
        os.kill(pid, 0)
        return True                     # process exists → live
    except ProcessLookupError:
        return False                    # owner gone → reclaim
    except PermissionError:
        return True                     # exists (not ours) → assume live


def main():
    config.HOME.mkdir(parents=True, exist_ok=True)
    if _lock_is_live():
        return
    _GLOBAL_LOCK.write_text(str(os.getpid()))
    try:
        engine.run_all()
    finally:
        _GLOBAL_LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
