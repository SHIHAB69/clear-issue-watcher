"""The Claude Code runtime seam — the only place that knows we're driving
Claude Code. Kept isolated so a different agent could be swapped later.

Runs ONE turn for an event in the source's rolling session (resumed), returns
(ok, session_id). ok=False means the turn failed (retry) — the event is not
popped by the caller.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_TURNS = "150"
TIMEOUT_S = 3600

SIGNATURE = "automated triage by watcher"

_AUTH_MARKERS = ("invalid api key", "authentication", "unauthorized", "401",
                 "please run /login", "oauth token", "expired", "invalid, expired")


def claude_bin() -> str:
    return shutil.which("claude") or "claude"


def _base_brief() -> str:
    """Platform-agnostic brief. The project's own CLAUDE.md (via cwd) is the
    authoritative context; this only carries behaviour rules."""
    return (Path(__file__).resolve().parent / "triage-prompt.md").read_text()


def _timed_input(prompt: str, timeout: int = 10) -> str | None:
    """Prompt on the terminal, return the typed line, or None on timeout / no TTY."""
    import sys
    import select
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print(prompt, end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        return None
    if ready:
        return sys.stdin.readline().strip()
    print("  (no answer — proceeding autonomously)")
    return None


def _one_turn(cmd, cwd, env):
    """Run a single claude turn, return (ok, session_id, result_text)."""
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return False, "", "__timeout__"
    if res.returncode != 0:
        return False, "", (res.stdout or res.stderr or "")[-200:]
    sid, result = "", ""
    try:
        out = json.loads(res.stdout)
        sid = out.get("session_id", "") or ""
        result = out.get("result") or ""
    except Exception:
        result = (res.stdout or "")[-400:]
    return True, sid, result


def run_event(source: "config.Source", adapter, event_dict: dict,
              interactive: bool = False) -> tuple[bool, str]:
    resume_id = source.session_id()
    event_json = json.dumps(event_dict, indent=1)

    mode = source.mode()
    mode_note = (
        f"\n\nCURRENT MODE: {mode}. "
        + ("triage = investigate and post at most ONE comment; do NOT make changes "
           "(no status/assign/edits/commits/deploys) — describe the fix plan instead."
           if mode != "full" else
           "full = you may act (comment, and where the platform/hard-limits allow, "
           "change status/assign/implement) — the hard limits still hold.")
    )

    if resume_id:
        body = (
            "Next event to handle on this source. You already hold full context "
            "and the running status of every ticket from earlier in this session. "
            "Apply the same rules and decision tree." + mode_note
            + "\nHandle ONLY this event, then stop.\n\nEVENT:\n" + event_json
        )
    else:
        body = (
            _base_brief()
            + "\n\n## This source\n"
            + adapter.prompt_section()
            + mode_note
            + "\n\nEVENT:\n"
            + event_json
        )

    import os
    env = {**os.environ, **adapter.env()}
    config.log(f"[{source.slug}] handle start: {event_dict.get('kind')} "
               f"{event_dict.get('external_id')} (resume={resume_id[:8] or 'new'})")

    def _cmd(prompt, resume):
        c = [claude_bin(), "-p", prompt,
             "--allowedTools", ",".join(adapter.allowed_tools()),
             "--output-format", "json", "--max-turns", MAX_TURNS]
        if resume:
            c += ["--resume", resume]
        return c

    sid = resume_id
    prompt = body
    for _hop in range(4):        # allow a few approval round-trips per event
        ok, new_sid, result = _one_turn(_cmd(prompt, sid), adapter.cwd(), env)
        if not ok:
            if result == "__timeout__":
                config.log(f"[{source.slug}] handle TIMEOUT")
            else:
                hint = " — AUTH may be expired; re-login" if any(m in result.lower() for m in _AUTH_MARKERS) else ""
                config.log(f"[{source.slug}] handle FAILED{hint} :: {result}")
            return False, sid
        sid = new_sid or sid
        source.set_session_id(sid)

        # cooperative approval: did the agent ask for input?
        marker = "NEEDS_INPUT:"
        if marker in result:
            question = result.split(marker, 1)[1].strip().splitlines()[0]
            if interactive:
                print(f"\n🟡 [{source.slug}] {question}")
                ans = _timed_input("   your answer (10s, blank = let it proceed): ", 10)
                prompt = (f"Operator answered: {ans}" if ans
                          else "No operator answer — proceed on your best judgment "
                               "within the hard limits, and record what you did.")
            else:
                prompt = ("No operator is attached — proceed on your best judgment "
                          "within the hard limits, and record what you did.")
            continue
        break

    tail = (result or "")[-300:].replace("\n", " | ")
    with config.SESSIONS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{source.slug}"
                f"\t{event_dict.get('kind')}\t{sid}\n")
    config.log(f"[{source.slug}] handle done sid={sid} :: {tail}")
    return True, sid
