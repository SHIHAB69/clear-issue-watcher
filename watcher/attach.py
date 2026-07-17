"""The live, Claude-Code-like foreground view: `watcher` (bare, when sources
exist) or `watcher attach <slug>`.

It drives the source in interactive mode — you SEE Claude read/think/act live
(streamed), and when it asks `NEEDS_INPUT:` you get a terminal prompt with a
10s window. It keeps watching on a short poll until you Ctrl-C. The background
runner (if started) and this share a per-source lock, so they never overlap.
"""
import time

from . import config, engine

C_DIM, C_YEL, C_RESET = "\033[2m", "\033[33m", "\033[0m"
POLL_S = 60


def attach(slug: str):
    src = config.Source(slug)
    if not src.meta:
        print(f"✗ No source '{slug}'. See: watcher list")
        return
    ident = src.meta.get("repo") or src.meta.get("solution_name") or ""
    print(f"{C_YEL}╭─ watcher · {slug} · {ident} · mode={src.mode()}{C_RESET}")
    print(f"{C_YEL}│  live — you'll be prompted if it needs you. Ctrl-C to detach.{C_RESET}")
    print(f"{C_YEL}╰{'─'*50}{C_RESET}")
    try:
        while True:
            before = len(src.queue())
            engine.run_source(slug, interactive=True)   # streams live; handles approvals
            if before == 0 and len(src.queue()) == 0:
                for remaining in range(POLL_S, 0, -1):
                    print(f"\r{C_DIM}idle — next check in {remaining:>2}s "
                          f"(Ctrl-C to detach){C_RESET}", end="", flush=True)
                    time.sleep(1)
                print("\r" + " " * 50 + "\r", end="")
    except KeyboardInterrupt:
        print(f"\n{C_YEL}detached. Background runner keeps going if started "
              f"(watcher status).{C_RESET}")
