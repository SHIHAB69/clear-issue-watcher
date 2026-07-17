# watcher

A persistent runtime for **Claude Code** that watches your task sources —
**GitHub** repos and **Jetrix** projects (more adapters later) — and handles
each new issue / comment / activity like a senior engineer: it orients itself to
*your* project from its code, docs, and existing tickets, then triages, comments,
updates, and (in full mode, for code projects) fixes.

It's generic. It ships with **no built-in knowledge of any business** — every
source's own `CLAUDE.md`, docs, and tickets are the authoritative context, and
the agent learns the domain from them. Runs autonomously in the background;
`watcher attach` gives you a live, steerable session with an approval prompt.

Polls on a timer (a laptop can't receive webhooks), queues events, and hands
them **one at a time** to a **per-source rolling Claude session** so context
accumulates without clients ever mixing.

---

## Install (one command)

macOS / Linux:
```bash
curl -fsSL https://raw.githubusercontent.com/SHIHAB69/clear-issue-watcher/master/install.sh | bash
```
Windows (PowerShell):
```powershell
irm https://raw.githubusercontent.com/SHIHAB69/clear-issue-watcher/master/install.ps1 | iex
```
Prereqs: `python3`, `claude` on PATH, and `gh` (only for GitHub sources).
Then `watcher doctor` to verify.

## Add a source
```bash
cd ~/your-project && watcher        # GitHub: auto-detects repo + your gh login from the folder
# or:
watcher add                          # choose Jetrix → base URL → paste API key → pick a project
```
GitHub sources have a local repo, so they support `full` (fix) mode. Jetrix
sources act on tasks (comment / status / assign), no code fixes.

## Run it
```bash
watcher run-once <slug>     # one cycle now (headless test)
watcher start               # install the background runner (default every 120s)
watcher stop                # remove it
watcher status              # scheduler + sources
watcher list                # sources, mode, queue depth
watcher logs -f             # live event log
watcher attach <slug>       # drive one source in the foreground WITH approval prompts
watcher mode <slug> full    # triage (comment-only) ↔ full (act)
```

## Modes
- **triage** (default, safe): investigate and post at most one comment; no changes.
- **full**: act — comment, status/assign (Jetrix), or implement/commit within the
  hard limits (GitHub). Hard limits are never crossed autonomously: no security/
  destructive/irreversible ops, no force-push, no closing tickets.

## Interactive approval (`watcher attach`)
When you attach, the agent can pause on a consequential step and ask
`NEEDS_INPUT: <question>` — you get a terminal prompt with a 10-second window.
Answer to steer it; stay silent and it proceeds on its own and logs what it did.
In the background (no one attached) it never stalls — it just proceeds and records.

## `@watcher` / `@claude` directives
Address the agent in any comment to give it a priority instruction for that
ticket (from anyone). It judges consequence, not author — high-stakes asks are
deferred to you rather than executed.

## How it stays reliable
Per-source isolated state (queue, rolling session, mode); serial one-at-a-time
processing; events pop only on success (retry, capped); offline/asleep catch-up
via a persisted `last_checked`; auth-expiry detected with a re-login hint;
rolling-session compaction planned for longevity. State lives in `~/.watcher/`.

## Files
`watcher/` — engine (`engine`, `runtime`, `config`), adapters (`github`,
`jetrix`), CLI, scheduler, attach. `triage-prompt.md` — the platform-agnostic
brief. `DESIGN.md` — the full spec.

## Adding a platform later
Implement one `Adapter` (discover / prompt_section / allowed_tools / identity)
in `watcher/adapters/` and register it in `adapters/__init__.py`. The engine,
queue, sessions, modes, scheduling, and UI are unchanged.
