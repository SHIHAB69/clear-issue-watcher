"""Entry point the OS scheduler invokes each interval: run one cycle over all
sources, headless (non-interactive → autonomous). A global lock prevents two
cycles overlapping if one runs long."""
import os
import time

from . import config, engine

_GLOBAL_LOCK = config.HOME / "cycle.lock"


def main():
    config.HOME.mkdir(parents=True, exist_ok=True)
    if _GLOBAL_LOCK.exists() and time.time() - _GLOBAL_LOCK.stat().st_mtime < 3600:
        return
    _GLOBAL_LOCK.write_text(str(os.getpid()))
    try:
        engine.run_all()
    finally:
        _GLOBAL_LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
