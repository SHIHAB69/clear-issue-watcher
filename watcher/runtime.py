"""The Claude Code runtime seam — the only place that knows we're driving
Claude Code. Kept isolated so a different agent could be swapped later.

Runs ONE turn for an event in the source's rolling session (resumed), returns
(ok, session_id). ok=False means the turn failed (retry) — the event is not
popped by the caller.
"""
import json
import shutil
import subprocess
import threading
from collections import deque
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


def _timed_input(prompt: str, timeout: int = 10, timeout_note: str = "") -> str | None:
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
    if timeout_note:
        print(timeout_note)
    else:
        print()          # clean newline after the prompt
    return None


_C_DIM, _C_CYAN, _C_GREEN, _C_RESET = "\033[2m", "\033[36m", "\033[32m", "\033[0m"


def _out(line: str, emit) -> None:
    if emit:
        emit(line)
    else:
        print(line)


def _render_stream_event(obj, emit=None) -> None:
    """Render one stream-json event live. Prints, or sends to `emit(text)`."""
    t = obj.get("type")
    if t == "assistant":
        for c in (obj.get("message") or {}).get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                _out(f"💬 {c['text'].strip()}" if emit
                     else f"{_C_GREEN}💬 {c['text'].strip()}{_C_RESET}", emit)
            elif c.get("type") == "tool_use":
                arg = c.get("input", {}).get("command") or c.get("input", {}).get("file_path") \
                    or c.get("input", {}).get("body") or ""
                _out(f"🔧 {c.get('name','?')} {str(arg)[:150]}" if emit
                     else f"{_C_CYAN}🔧 {c.get('name','?')}{_C_RESET} {_C_DIM}{str(arg)[:150]}{_C_RESET}", emit)
    elif t == "user":
        for c in (obj.get("message") or {}).get("content", []):
            if c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                s = str(body or "").strip().replace("\n", " ")[:160]
                if s:
                    _out(f"   ↳ {s}" if emit else f"{_C_DIM}   ↳ {s}{_C_RESET}", emit)


def _stream_turn(cmd, cwd, env, emit=None):
    """Run a turn with live streaming output. Returns (ok, session_id, result_text)."""
    scmd = cmd + ["--output-format", "stream-json", "--verbose"]
    sid, result = "", ""
    diag = deque(maxlen=20)          # keep last non-JSON (stderr) lines for diagnostics
    try:
        # stderr→stdout so a full stderr pipe can't deadlock while we read stdout;
        # non-JSON (stderr) lines are captured in `diag` and skipped by the parser.
        proc = subprocess.Popen(scmd, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception as e:  # noqa: BLE001
        return False, "", str(e)
    # watchdog: a fully silent hang blocks `for line in proc.stdout` forever;
    # kill after TIMEOUT_S so the worker can't wedge. proc.kill() sends EOF → loop ends.
    timed_out = threading.Event()
    watchdog = threading.Timer(TIMEOUT_S, lambda: (timed_out.set(), proc.kill()))
    watchdog.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                diag.append(line)
                continue
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                sid = obj.get("session_id", sid) or sid
            elif obj.get("type") == "result":
                sid = obj.get("session_id", sid) or sid
                result = obj.get("result") or ""
            else:
                _render_stream_event(obj, emit)
        proc.wait()
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        return False, sid, "__timeout__"
    if proc.returncode != 0:
        tail = " | ".join(diag)
        return False, sid, (f"rc={proc.returncode} :: {tail}" if tail
                            else f"stream turn exited rc={proc.returncode}")
    return True, sid, result


def _one_turn(cmd, cwd, env):
    """Run a single captured (non-streaming) claude turn → (ok, session_id, result)."""
    cmd = cmd + ["--output-format", "json"]
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


def chat(source: "config.Source", adapter, message: str, emit=None) -> bool:
    """Send an operator message into the source's live session and stream the
    reply. Framed as an OPERATOR MESSAGE, not an issue comment — Claude treats
    it as extra instruction/context, never posts it as a ticket comment."""
    import os
    resume_id = source.session_id()
    env = {**os.environ, **adapter.env()}
    framed = ("[OPERATOR MESSAGE — this is the human operator talking to you "
              "directly, NOT a comment on any ticket. Do not post it anywhere. "
              "Treat it as instruction/question/context]:\n" + message)
    if resume_id:
        cmd = [claude_bin(), "-p", framed,
               "--allowedTools", ",".join(adapter.allowed_tools()),
               "--max-turns", MAX_TURNS, "--resume", resume_id]
    else:
        prompt = (_base_brief() + "\n\n## This source\n" + adapter.prompt_section()
                  + "\n\n" + framed)
        cmd = [claude_bin(), "-p", prompt,
               "--allowedTools", ",".join(adapter.allowed_tools()),
               "--max-turns", MAX_TURNS]
    ok, sid, result = _stream_turn(cmd, adapter.cwd(), env, emit)
    if sid:
        source.set_session_id(sid)
    if not ok:
        hint = " — AUTH may be expired; re-login" if any(
            m in (result or "").lower() for m in _AUTH_MARKERS) else ""
        config.log(f"[{source.slug}] chat FAILED{hint} :: {result}")
    return ok


def compact(source: "config.Source", adapter) -> bool:
    """Summarize the rolling session into a durable memory doc, then start fresh.
    Keeps long-lived sessions from growing slow/stale while preserving knowledge.
    """
    resume_id = source.session_id()
    if not resume_id:
        return False
    import os
    env = {**os.environ, **adapter.env()}
    prompt = (
        "Compaction step (not a ticket). Distill everything important you've "
        "learned about THIS project/source in your session so far into a concise "
        "engineering-memory doc a fresh session could load to be immediately "
        "effective: architecture & domain, conventions, stakeholders/who's who, "
        "recurring issues, decisions made, and any open/unfinished threads. "
        "Output ONLY the memory doc in markdown, no preamble."
    )
    cmd = [claude_bin(), "-p", prompt, "--output-format", "json",
           "--max-turns", "20", "--resume", resume_id]
    ok, _sid, result = _one_turn(cmd, adapter.cwd(), env)
    if ok and result.strip():
        source.set_memory(result.strip())
        source.clear_session()          # next event starts fresh, loads memory
        config.log(f"[{source.slug}] compacted session {resume_id[:8]} → memory.md")
        return True
    config.log(f"[{source.slug}] compaction skipped (turn failed)")
    return False


def run_event(source: "config.Source", adapter, event_dict: dict,
              interactive: bool = False, emit=None, ask=None) -> tuple[bool, str]:
    resume_id = source.session_id()
    event_json = json.dumps(event_dict, indent=1)

    # operator message injected from the UI — not a ticket, extra instruction/context
    if event_dict.get("kind") == "user_message":
        ok = chat(source, adapter, event_dict.get("data", {}).get("text", ""), emit=emit)
        return ok, source.session_id()   # propagate failure so it retries, not silently lost

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
        mem = source.memory()
        mem_note = (f"\n\n## Carried memory (from earlier sessions on this source)\n{mem}\n"
                    if mem.strip() else "")
        body = (
            _base_brief()
            + "\n\n## This source\n"
            + adapter.prompt_section()
            + mem_note
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
             "--max-turns", MAX_TURNS]
        if resume:
            c += ["--resume", resume]
        return c

    streaming = interactive or emit is not None
    turn = (lambda c, w, e: _stream_turn(c, w, e, emit)) if streaming else _one_turn
    sid = resume_id
    prompt = body
    for _hop in range(4):        # allow a few approval round-trips per event
        ok, new_sid, result = turn(_cmd(prompt, sid), adapter.cwd(), env)
        if not ok:
            if result == "__timeout__":
                config.log(f"[{source.slug}] handle TIMEOUT")
            else:
                hint = " — AUTH may be expired; re-login" if any(m in result.lower() for m in _AUTH_MARKERS) else ""
                config.log(f"[{source.slug}] handle FAILED{hint} :: {result}")
            return False, sid
        sid = new_sid or sid
        source.set_session_id(sid)

        # cooperative approval: the agent asks by ending with a CONTROL LINE
        # (last non-empty line) — not a stray mention in prose/quotes/code.
        # protocol: NEEDS_INPUT: <question> [:: option1 :: option2 ...]
        marker = "NEEDS_INPUT:"
        rlines = [ln.strip() for ln in (result or "").splitlines() if ln.strip()]
        if rlines and rlines[-1].startswith(marker):
            raw = rlines[-1][len(marker):].strip()
            if not raw:                      # bare marker, no question → nothing to ask
                break
            parts = [p.strip() for p in raw.split("::") if p.strip()]
            question = parts[0] if parts else raw
            options = parts[1:]
            answer = None
            if ask is not None:              # TUI: arrow-key "are you there?" → question
                answer = ask(question, options)
            elif interactive:                # plain terminal fallback
                _out(f"🟡 {question}" + (f"  options: {options}" if options else ""), emit)
                answer = _timed_input("   your answer (10s, blank = proceed): ", 10,
                                      timeout_note="   (no answer — proceeding on best judgment)")
            prompt = (f"Operator answered: {answer}" if answer
                      else "No operator answer — take the SAFE path on your best judgment "
                           "(do NOT perform dangerous/irreversible actions); if a dangerous "
                           "action is what's needed, post a comment explaining what you propose, "
                           "why, and the risk, and ask for approval on the ticket.")
            continue
        break
    else:
        # loop exhausted with the last hop STILL asking — the ask was never resolved.
        # Do not report success; return False so drain_queue retries the same session.
        tail = (result or "")[-300:].replace("\n", " | ")
        config.log(f"[{source.slug}] handle UNRESOLVED — approval cap exhausted, "
                   f"still awaiting input :: {tail}")
        return False, sid

    tail = (result or "")[-300:].replace("\n", " | ")
    with config.SESSIONS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{source.slug}"
                f"\t{event_dict.get('kind')}\t{sid}\n")
    config.log(f"[{source.slug}] handle done sid={sid} :: {tail}")
    return True, sid
