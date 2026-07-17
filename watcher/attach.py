"""The live, interactive foreground view: `watcher` (bare, when sources exist)
or `watcher attach <slug>`.

Loop: process any queued events (streamed live, with the 10s approval prompt),
then show a real input line. You can:
  - type a message  → it's sent into the live session and the reply streams (chat)
  - /mode full|triage, /status, /help, /quit
  - just press Enter or wait → it polls the source for new events
Keystrokes are consumed by the prompt (no more raw-echo leak).

Note: this is turn-based chat (between/after runs), not mid-stream typing —
`claude -p` can't accept input while a turn is streaming. The 10s go/no-go
approval appears during a run when the agent pauses with NEEDS_INPUT.
"""
from . import config, engine, runtime
from .adapters import build_adapter

C_DIM, C_YEL, C_RESET = "\033[2m", "\033[33m", "\033[0m"
POLL_S = 60

HELP = ("commands:  <text> = chat to the session   /mode full|triage   "
        "/status   /poll (check now)   /help   /quit")


def attach(slug: str):
    src = config.Source(slug)
    if not src.meta:
        print(f"✗ No source '{slug}'. See: watcher list")
        return
    ident = src.meta.get("repo") or src.meta.get("solution_name") or ""
    adapter = build_adapter(src.meta)
    print(f"{C_YEL}╭─ watcher · {slug} · {ident} · mode={src.mode()}{C_RESET}")
    print(f"{C_YEL}│  live — type to chat, or wait; you'll get a 10s go/no-go if it asks.{C_RESET}")
    print(f"{C_YEL}│  {HELP}{C_RESET}")
    print(f"{C_YEL}╰{'─'*58}{C_RESET}")
    try:
        while True:
            engine.run_source(slug, interactive=True)      # handle queued events (streamed)
            line = runtime._timed_input(f"{C_DIM}watcher> (type, or wait {POLL_S}s to poll {ident}){C_RESET} ", POLL_S)
            if line is None:                                # timeout → poll again
                continue
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit", "q"):
                break
            if line == "/help":
                print(HELP); continue
            if line == "/status":
                print(f"  {slug} · {ident} · mode={src.mode()} · queued={len(src.queue())}")
                continue
            if line == "/poll":
                continue                                    # loop top re-runs discovery
            if line.startswith("/mode"):
                parts = line.split()
                if len(parts) == 2 and parts[1] in ("triage", "full"):
                    src.set_mode(parts[1]); print(f"  mode → {parts[1]}")
                else:
                    print("  usage: /mode full|triage")
                continue
            # anything else → chat into the session, streamed
            runtime.chat(src, adapter, line)
    except KeyboardInterrupt:
        print(f"\n{C_YEL}detached. Background runner keeps going if started (watcher status).{C_RESET}")
