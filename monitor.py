#!/usr/bin/env python3
"""Live monitor for the issue watcher — run in a separate terminal:

    python3 scripts/issue-watcher/monitor.py

Streams, in real time:
  * watcher events (from ~/.clear-issue-watcher/watcher.log)
  * the full activity of any triage session currently running — every tool
    call and every assistant message, pretty-printed as it happens (read live
    from the Claude Code session transcript on disk).

Ctrl-C to quit. To replay a finished run in the full interactive UI:
    scripts/issue-watcher/sessions.sh          # find the SESSION id
    claude --resume <SESSION>
"""
import json, os, time
from pathlib import Path

WATCH_LOG = Path.home() / ".clear-issue-watcher/watcher.log"
MODE_FILE = Path.home() / ".clear-issue-watcher/mode"
# claude stores session transcripts per-project; this is the repo's project dir
import json, re as _re
_cfg = Path.home() / ".clear-issue-watcher/config.json"
_pd  = json.loads(_cfg.read_text())["project_dir"] if _cfg.exists() else str(Path.cwd())
# Claude Code names its per-project transcript dir by mangling the path: / and . -> -
PROJ_DIR = Path.home() / ".claude/projects" / _re.sub(r"[/.]", "-", _pd)

C_DIM, C_CYAN, C_GREEN, C_YELLOW, C_RESET = "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"


SESS_PTR = Path.home() / ".clear-issue-watcher/session"

def newest_active_transcript(within_s: int = 120):
    """The rolling session's transcript, while it is actively being written."""
    if not SESS_PTR.exists():
        return None
    sid = SESS_PTR.read_text().strip()
    if not sid:
        return None
    p = PROJ_DIR / f"{sid}.jsonl"
    if not p.exists():
        # resume may fork to a new id mid-run; fall back to newest recent file
        try:
            files = sorted(PROJ_DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)
        except FileNotFoundError:
            return None
        p = files[-1] if files else None
        if p is None:
            return None
    return p if time.time() - p.stat().st_mtime < within_s else None


def render(line: str) -> str | None:
    try:
        e = json.loads(line)
    except Exception:
        return None
    msg = e.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    out = []
    for c in content:
        t = c.get("type")
        if t == "text" and c.get("text", "").strip():
            out.append(f"{C_GREEN}💬 {c['text'].strip()[:400]}{C_RESET}")
        elif t == "tool_use":
            name = c.get("name", "?")
            inp = c.get("input", {})
            arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            out.append(f"{C_CYAN}🔧 {name}{C_RESET} {C_DIM}{str(arg)[:160]}{C_RESET}")
        elif t == "tool_result":
            body = c.get("content")
            if isinstance(body, list):
                body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
            snippet = str(body or "").strip().replace("\n", " ")[:200]
            if snippet:
                out.append(f"{C_DIM}   ↳ {snippet}{C_RESET}")
    return "\n".join(out) if out else None


def follow(path: Path, pos: int):
    with path.open() as f:
        f.seek(pos)
        for line in f:
            yield line
        pos = f.tell()
    return


def main():
    mode = MODE_FILE.read_text().strip() if MODE_FILE.exists() else "triage"
    print(f"{C_YELLOW}── Clear issue-watcher monitor ── mode: {mode} ── Ctrl-C to quit ──{C_RESET}")
    log_pos = WATCH_LOG.stat().st_size if WATCH_LOG.exists() else 0
    session_path, session_pos = None, 0
    while True:
        # watcher log lines
        if WATCH_LOG.exists() and WATCH_LOG.stat().st_size > log_pos:
            with WATCH_LOG.open() as f:
                f.seek(log_pos)
                for line in f:
                    print(f"{C_YELLOW}▶ {line.rstrip()}{C_RESET}")
                log_pos = f.tell()
        # active session stream
        active = newest_active_transcript()
        if active is not None:
            if active != session_path:
                session_path, session_pos = active, 0
                print(f"{C_YELLOW}── streaming session {active.stem[:8]}… ──{C_RESET}")
            size = active.stat().st_size
            if size > session_pos:
                with active.open() as f:
                    f.seek(session_pos)
                    for line in f:
                        r = render(line)
                        if r:
                            print(r)
                    session_pos = f.tell()
        time.sleep(1.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
