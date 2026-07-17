"""`watcher attach <slug>` — foreground interactive session for one source:
processes its queue with the cooperative approval prompt enabled, and streams a
live view of the log + the active Claude session transcript.

Two behaviours in one command:
  - drives the source interactively (run_source with interactive=True), so the
    agent's NEEDS_INPUT pauses become terminal prompts you can answer;
  - between/after that, tails the log so you see what's happening.

For pure observation of the background runner, use `watcher logs -f`.
"""
import json
import re
import time
from pathlib import Path

from . import config, engine

C_DIM, C_CYAN, C_GREEN, C_YELLOW, C_RESET = "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"
PROJ = Path.home() / ".claude/projects"


def _transcript_for(session_id: str) -> Path | None:
    if not session_id:
        return None
    for d in PROJ.glob("*"):
        p = d / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


def _render(line: str) -> str | None:
    try:
        e = json.loads(line)
    except Exception:
        return None
    content = (e.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    out = []
    for c in content:
        t = c.get("type")
        if t == "text" and c.get("text", "").strip():
            out.append(f"{C_GREEN}💬 {c['text'].strip()[:400]}{C_RESET}")
        elif t == "tool_use":
            arg = c.get("input", {}).get("command") or c.get("input", {}).get("file_path") or ""
            out.append(f"{C_CYAN}🔧 {c.get('name','?')}{C_RESET} {C_DIM}{str(arg)[:150]}{C_RESET}")
    return "\n".join(out) if out else None


def attach(slug: str):
    src = config.Source(slug)
    if not src.meta:
        print(f"✗ No source '{slug}'. See: watcher list")
        return
    print(f"{C_YELLOW}── watcher attach: {slug}  mode={src.mode()} ──{C_RESET}")
    print("Driving this source now; you'll be prompted if the agent asks. Ctrl-C to detach.\n")
    try:
        # interactive drive: NEEDS_INPUT pauses become prompts (runtime handles the 10s wait)
        engine.run_source(slug, interactive=True)
    except KeyboardInterrupt:
        print("\ndetached.")
        return
    # after driving, show the tail of what happened
    sid = src.session_id()
    tp = _transcript_for(sid)
    if tp:
        print(f"\n{C_YELLOW}── recent session activity ({sid[:8]}) ──{C_RESET}")
        for line in tp.read_text().splitlines()[-40:]:
            r = _render(line)
            if r:
                print(r)
    print(f"\n{C_YELLOW}done. Live log: watcher logs -f{C_RESET}")
