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


def run_event(source: "config.Source", adapter, event_dict: dict) -> tuple[bool, str]:
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

    cmd = [claude_bin(), "-p", body,
           "--allowedTools", ",".join(adapter.allowed_tools()),
           "--output-format", "json", "--max-turns", MAX_TURNS]
    if resume_id:
        cmd += ["--resume", resume_id]

    import os
    env = {**os.environ, **adapter.env()}
    config.log(f"[{source.slug}] handle start: {event_dict.get('kind')} "
               f"{event_dict.get('external_id')} (resume={resume_id[:8] or 'new'})")
    try:
        res = subprocess.run(cmd, cwd=adapter.cwd(), capture_output=True,
                             text=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        config.log(f"[{source.slug}] handle TIMEOUT {event_dict.get('external_id')}")
        return False, resume_id

    blob = ((res.stdout or "") + " " + (res.stderr or "")).lower()
    if res.returncode != 0:
        hint = " — AUTH may be expired; re-login" if any(m in blob for m in _AUTH_MARKERS) else ""
        config.log(f"[{source.slug}] handle FAILED rc={res.returncode}{hint} "
                   f":: {(res.stdout or res.stderr or '')[-200:]}")
        return False, resume_id

    sid, tail = resume_id, ""
    try:
        out = json.loads(res.stdout)
        sid = out.get("session_id", resume_id) or resume_id
        tail = (out.get("result") or "")[-300:].replace("\n", " | ")
    except Exception:
        tail = (res.stdout or "")[-300:].replace("\n", " | ")

    source.set_session_id(sid)
    with config.SESSIONS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{source.slug}"
                f"\t{event_dict.get('kind')}\t{sid}\n")
    config.log(f"[{source.slug}] handle done sid={sid} :: {tail}")
    return True, sid
