#!/usr/bin/env python3
"""Clear issue watcher — one rolling Claude Code session, serial event queue.

Polls nicorogers/clear.server. Every new issue and every human comment is
appended to a FIFO queue and handled ONE AT A TIME by a single, persistent
Claude Code session (resumed each time) so the agent keeps full context and
knows the running status of every ticket.

Anti-loop: the bot posts comments signed with SIGNATURE (as SHIHAB69). We skip
only comments that carry that signature — real human comments from any account,
including SHIHAB69, DO trigger (so Shihab can test by commenting himself).
"""
import json, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

import shutil

TOOL_DIR  = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".clear-issue-watcher"
STATE     = STATE_DIR / "state.json"
QUEUE     = STATE_DIR / "queue.jsonl"       # pending events, FIFO
SESS_PTR  = STATE_DIR / "session"           # id of the rolling session to resume
LOCK      = STATE_DIR / "lock"
LOG       = STATE_DIR / "watcher.log"
SESSIONS  = STATE_DIR / "sessions.tsv"
MODE_FILE = STATE_DIR / "mode"
CONFIG    = STATE_DIR / "config.json"
PROMPT    = TOOL_DIR / "triage-prompt.md"

# --- config: what repo to watch, what local project to work in --------------
# ~/.clear-issue-watcher/config.json  (created by the README's setup prompt):
#   { "github_repo": "owner/name",
#     "project_dir": "/abs/path/to/local/checkout",   # cwd for Claude = its CLAUDE.md
#     "operator_login": "your-gh-login",   # optional; auto-detected via `gh api user`
#     "claude_bin": "/abs/path/to/claude"  # optional; auto-detected via PATH
#   }
def _load_config():
    if not CONFIG.exists():
        raise SystemExit(f"missing {CONFIG} — run the README setup prompt first")
    cfg = json.loads(CONFIG.read_text())
    if not cfg.get("github_repo") or not cfg.get("project_dir"):
        raise SystemExit(f"{CONFIG} needs 'github_repo' and 'project_dir'")
    if not cfg.get("operator_login"):
        r = subprocess.run(["gh", "api", "user", "-q", ".login"],
                           capture_output=True, text=True)
        cfg["operator_login"] = r.stdout.strip()
    if not cfg.get("claude_bin"):
        cfg["claude_bin"] = shutil.which("claude") or "claude"
    return cfg

_cfg      = _load_config()
REPO      = _cfg["github_repo"]
PROJECT_DIR = Path(_cfg["project_dir"])
ME        = _cfg["operator_login"]
CLAUDE    = _cfg["claude_bin"]
SIGNATURE = "automated triage by Shihab's Claude Code"

MODE = MODE_FILE.read_text().strip() if MODE_FILE.exists() else "triage"

TOOLS_TRIAGE = [
    "Read", "Grep", "Glob",
    "Bash(gh issue view:*)", "Bash(gh issue comment:*)", "Bash(gh issue edit:*)",
    f"Bash(gh api repos/{REPO}:*)", "Bash(gh auth token:*)",
    f"Bash(curl -sL -o {STATE_DIR}/scratch/:*)",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)",
]
TOOLS_FULL = TOOLS_TRIAGE + [
    "Write", "Edit", "TodoWrite",
    "Bash(psql:*)", "Bash(source .env && psql:*)",
    "Bash(source .env && supabase functions deploy:*)", "Bash(supabase functions deploy:*)",
    "Bash(createdb:*)", "Bash(dropdb:*)",
    "Bash(/opt/homebrew/opt/postgresql@17/bin/pg_dump:*)",
    "Bash(curl:*)", "Bash(deno check:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git push origin main)",
]
ALLOWED_TOOLS = ",".join(TOOLS_FULL if MODE == "full" else TOOLS_TRIAGE)


def log(msg):
    STATE_DIR.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def gh_json(args):
    out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout) if out.stdout.strip() else []


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "processed": []}


def save_state(s):
    s["processed"] = s["processed"][-800:]
    STATE.write_text(json.dumps(s, indent=1))


def enqueue(event):
    with QUEUE.open("a") as f:
        f.write(json.dumps(event) + "\n")


def read_queue():
    if not QUEUE.exists():
        return []
    return [json.loads(l) for l in QUEUE.read_text().splitlines() if l.strip()]


def write_queue(events):
    QUEUE.write_text("".join(json.dumps(e) + "\n" for e in events))


def discover(state):
    """Find new issues + comments since last check; append to the FIFO queue."""
    since = state["last_checked"]
    found = []

    for it in gh_json(["api", f"repos/{REPO}/issues", "-X", "GET",
                       "-f", f"since={since}", "-f", "state=all", "-f", "per_page=50"]):
        if "pull_request" in it:
            continue
        key = f"issue-{it['id']}"
        if key in state["processed"] or it["created_at"] < since:
            continue
        state["processed"].append(key)
        found.append({"key": key, "ts": it["created_at"], "type": "new_issue",
                      "issue_number": it["number"], "title": it["title"],
                      "author": it["user"]["login"], "url": it["html_url"]})

    for c in gh_json(["api", f"repos/{REPO}/issues/comments", "-X", "GET",
                     "-f", f"since={since}", "-f", "per_page=50"]):
        key = f"comment-{c['id']}"
        if key in state["processed"] or c["created_at"] < since:
            continue
        state["processed"].append(key)
        # anti-loop: skip ONLY the bot's own signed comments
        if c["user"]["login"] == ME and SIGNATURE in (c["body"] or ""):
            continue
        issue_number = int(c["issue_url"].rstrip("/").rsplit("/", 1)[-1])
        body = c["body"] or ""
        # a comment addressed to @watcher / @claude is a direct instruction
        directive = any(tok in body.lower() for tok in ("@watcher", "@claude"))
        found.append({"key": key, "ts": c["created_at"], "type": "new_comment",
                      "issue_number": issue_number, "comment_author": c["user"]["login"],
                      "comment_body": body[:2000], "url": c["html_url"],
                      "directive_to_watcher": directive})

    # 3) other issue activity (assign, label, close, reopen, rename) until closed.
    #    The events API has no `since` filter, so we page recent + filter client-side.
    #    Anti-loop: skip events whose actor is the bot itself (its own assign/label).
    MEANINGFUL = {"assigned", "unassigned", "labeled", "unlabeled",
                  "closed", "reopened", "renamed", "milestoned", "demilestoned"}
    for ev in gh_json(["api", f"repos/{REPO}/issues/events", "-X", "GET", "-f", "per_page=100"]):
        if (ev.get("created_at") or "") <= since:
            continue
        key = f"event-{ev['id']}"
        if key in state["processed"]:
            continue
        state["processed"].append(key)
        action = ev.get("event")
        if action not in MEANINGFUL:
            continue
        if (ev.get("actor") or {}).get("login") == ME:      # bot's own action
            continue
        iss = ev.get("issue") or {}
        if "pull_request" in iss or not iss.get("number"):
            continue
        found.append({"key": key, "ts": ev["created_at"], "type": "issue_activity",
                      "issue_number": iss["number"], "action": action,
                      "actor": (ev.get("actor") or {}).get("login"),
                      "url": iss.get("html_url", "")})

    found.sort(key=lambda e: e["ts"])          # FIFO by creation time
    for e in found:
        enqueue(e)
    if found:
        log("queued " + str(len(found)) + " event(s): "
            + ", ".join(f"{e['type']}#{e['issue_number']}" for e in found))
    state["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)


def run_one(event):
    """Handle a single queued event in the ONE rolling session (resumed)."""
    resume_id = SESS_PTR.read_text().strip() if SESS_PTR.exists() else ""
    if resume_id:
        body = ("Next GitHub event to handle. You already hold full context and "
                "the running status of every ticket from earlier in this session. "
                "Apply the same rules and decision tree. Handle ONLY this event, "
                "then stop.\n\nEVENT:\n" + json.dumps(event, indent=1))
    else:
        body = PROMPT.read_text() + "\n\nEVENT:\n" + json.dumps(event, indent=1)

    cmd = [CLAUDE, "-p", body, "--allowedTools", ALLOWED_TOOLS,
           "--output-format", "json", "--max-turns", "150"]
    if resume_id:
        cmd += ["--resume", resume_id]

    log(f"handle start: {event['type']} #{event['issue_number']} "
        f"(resume={resume_id[:8] or 'new'})")
    try:
        res = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        log(f"handle TIMEOUT: #{event['issue_number']} — will retry next cycle")
        return False   # keep at head of queue

    blob = (res.stdout or "") + " " + (res.stderr or "")
    auth_expired = any(s in blob.lower() for s in
                       ("invalid api key", "authentication", "unauthorized",
                        "401", "please run /login", "oauth token", "expired"))
    if res.returncode != 0:
        hint = " — CLAUDE AUTH looks expired; run `claude` to re-login" if auth_expired else ""
        log(f"handle FAILED: #{event['issue_number']} rc={res.returncode}{hint} "
            f":: {blob.strip()[-200:]}")
        return False   # do NOT pop — retry after re-auth (capped in main)

    sid, tail = resume_id, ""
    try:
        out = json.loads(res.stdout)
        sid = out.get("session_id", resume_id) or resume_id
        tail = (out.get("result") or "")[-300:].replace("\n", " | ")
    except Exception:
        tail = (res.stdout or res.stderr or "")[-300:].replace("\n", " | ")
    if sid:
        SESS_PTR.write_text(sid)              # chain context forward
    with SESSIONS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t#{event['issue_number']}"
                f"\t{event['type']}\t{sid}\n")
    log(f"handle done: #{event['issue_number']} rc={res.returncode} "
        f"session={sid} result: {tail}")
    return True


def main():
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "scratch").mkdir(exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 3600:
        return
    LOCK.write_text(str(os.getpid()))
    try:
        state = load_state()
        discover(state)
        # drain the queue serially, oldest first, one session, one at a time
        MAX_ATTEMPTS = 5     # e.g. survives a re-auth gap; then give up so the
                             # queue can't be blocked forever by one bad event
        while True:
            q = read_queue()
            if not q:
                break
            event = q[0]
            event["attempts"] = event.get("attempts", 0) + 1
            LOCK.write_text(str(os.getpid()))   # refresh lock during long runs
            ok = run_one(event)
            if ok:
                write_queue(q[1:])              # pop only on success
            elif event["attempts"] >= MAX_ATTEMPTS:
                log(f"GIVING UP on #{event['issue_number']} after "
                    f"{event['attempts']} attempts — dropping from queue")
                write_queue(q[1:])
            else:
                q[0] = event                     # persist the attempt counter
                write_queue(q)
                break                            # retry this event next cycle
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
