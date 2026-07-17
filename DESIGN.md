# Watcher — final design (agreed spec)

A persistent runtime for Claude Code: it watches task sources (GitHub now,
Jetrix next), and handles each new issue/comment/activity like a senior
engineer — triaging, commenting, updating, assigning — one at a time, with
full per-source context. Runs autonomously in the background; you can attach a
live terminal UI to watch and steer it.

Positioning: not "a GitHub bot." An always-on engineering teammate that never
logs off. Command name stays `watcher`. Language stays Python.

---

## 1. Architecture — core engine + adapters

The engine is platform-agnostic. Each platform is an **adapter** implementing:
- `discover_events(since)` → new issues / comments / activity
- `fetch_context(event)` → full issue/task detail to reason over
- `post_comment(target, body)`, `set_status(...)`, `assign(...)`, `mention(...)` → write-backs
- `identity()` → who "I" am here (for assigned/mentions-me)

Adapters now: `GitHubAdapter` (today's `gh` logic, generalized), `JetrixAdapter`.
Later (cheap to add, do NOT build speculatively): ClickUp, Trello, Linear, etc.
The engine (queue, sessions, modes, scheduling, safety) never changes per platform.

**Runtime seam:** the single function that runs a Claude turn (`run_one`) is the
only place that knows it's Claude Code. Kept isolated so a different agent could
be swapped later — but we are NOT abstracting the runtime now (no value today,
and our session/compaction/safety model is built on Claude Code semantics).

## 2. State layout — per-source isolation
```
~/.watcher/
  config.json                # global: registered sources
  <source-slug>/             # e.g. github-nicorogers-clear.server, jetrix-<solutionId>
    state.json               # last_checked + processed keys
    queue.jsonl              # this source's FIFO events
    session                  # rolling Claude session id (chained)
    mode                     # triage | full
    scratch/                 # image/downloads
  watcher.log                # shared, source-tagged
  sessions.tsv               # run history (for `attach`/resume)
```
Each source has its OWN rolling session → no context bleed between clients, and
one source's queue can't block another's.

## 3. Engine loop (per cycle)
For each registered source, in turn: pin identity → `discover_events` → append
to that source's FIFO queue → drain serially (one event → resume that source's
session → act → pop only on success). Serial within and across sources: exactly
one Claude session active at a time. Per-source lock + stale-lock guard prevents
overlap. Event popped only on success; failures retry, capped (5) so one bad
event can't wedge the queue.

## 4. `watcher` CLI
- `watcher` (in a dir) → interactive add/setup for this source
- `watcher init` / `watcher doctor` → prereq checks (python, gh, claude, key) + fixes
- `watcher list` → sources, mode, last run, health
- `watcher add` / `watcher remove [source]`
- `watcher start` / `stop` / `restart` → install/tear down the OS scheduler (or daemon)
- `watcher attach [source]` → the live TUI (replaces standalone monitor.py)
- `watcher logs [-f]`
- `watcher mode triage|full [source]`

## 5. Install (like Claude Code)
- macOS/Linux: `curl -fsSL <url>/install.sh | bash`
- Windows: `irm <url>/install.ps1 | iex`
- Installs the `watcher` entry-point on PATH, prints the one PATH line to add.
  A package, not a repo clone. First run auto-triggers `doctor`.
- Distribution: PRIVATE (internal to the team) for now.

## 6. Cross-platform scheduling (Mac, Linux, Windows)
One abstraction, three backends, chosen at `watcher start`:
- macOS: launchd user agent
- Linux: systemd user timer (fallback: cron)
- Windows: Task Scheduler (`schtasks`, `pythonw`)
Carries per-source env (e.g. `GH_CONFIG_DIR` for a scoped gh identity).
`watcher doctor` verifies the scheduler/daemon is actually registered.

## 7. Setup flows
**GitHub** (has local code → supports triage AND full/fix):
- run `watcher` inside the project → auto-detect local path (cwd), repo (git
  remote), gh account. Warn clearly if not a git repo or gh not logged in.
**Jetrix** (issue tracker, no local code → triage/act on tasks, no code-fix/deploy):
- run `watcher`, pick Jetrix → paste a `jtx_live_…` key (or open Dev Docs page to
  mint one) → `/dev/me` + `/dev/solutions` → pick a solution = one source.

## 8. Jetrix adapter — locked specs (PR #177, dev-jetrix sandbox for testing)
- Auth: `x-api-key: jtx_live_…` on every call; stored per-source (NOT global).
- Identity: `GET /api/v1/dev/me` → `{id,email,name, isOrgAdmin}`.
- Projects: `GET /api/v1/dev/solutions` → `[{id,name,role,taskCount,updatedAt}]`
  (returns all org solutions when caller is org admin — the admin-bypass fix).
- **Trigger source (single):** `GET /api/v1/dev/solutions/:id/activity?since=<ISO>&before=&limit=`
  — newest-first merged feed of comments + activity, INCLUDING `type:"created"`
  for new tasks. Poll with `since=<max createdAt seen>`, page back via
  `nextBefore` to last-seen; dedupe by item `id`. Catches comments + status/
  assign/field changes + new tasks in one call. (Comments do NOT bump task
  `updatedAt`, which is why the feed — not `export?updatedSince` — is the trigger.)
- Context: `GET /api/v1/dev/solutions/:id/export` (per affected task) → rich
  `sections` (description, acceptanceCriteria, testScenarios, nfrs, businessRules,
  assumptions, implementation, …), assignees+emails, timestamps, subtasks,
  comments with `authorEmail`/`createdAt`/`body`/`mentions`(emails).
- Writes (as key owner, RBAC + audit intact):
  - Comment: `POST /api/v1/comment/create` `{solutionId, taskId(ObjectId), text, mentions:[userId], parentCommentId}`
  - Status: `PUT /api/v1/solutions/:solutionId/tasks/:taskNumber` `{status}`;
    values: todo, reopen, inProgress, agentExecuting, devReview, inQaReview, blocked, done
  - Assign (SET): `POST /api/v1/solutions/:solutionId/tasks/:taskNumber/assign` `{assigneeIds:[...]}`;
    unassign-all via PUT `{assigneeIds:[]}`
  - Mentions: pass `mentions:[userId]` AND put `@Name` in text for rendering
    (no server-side @-parse). userIds from `/dev/solutions` members or export assignees.
  - NOTE: comment uses ObjectId `taskId`; status/assign use numeric `taskNumber`.
    Export returns both.
- Mentions-me detection: comment `mentions` are emails in export/feed → match owner email.
- Anti-loop: bodies returned verbatim → signature-skip works unchanged.
- Ops: no rate limits (be polite in poll interval); `/activity` paginates,
  `/export` does not (bound with filters); expired/revoked key → HTTP 401
  `"Invalid, expired, or revoked API key"` → prompt to mint a new one.
- Open item: real production `app.jetrix.ai` data is NOT on this cluster; test
  against `dev-jetrix` sandbox now, real data once Selvam confirms the env + JWT_SECRET.

## 9. GitHub adapter — carried from today's watcher
Detect repo from git remote, identity from gh (scoped via `GH_CONFIG_DIR` so the
machine's global gh account can be different). Poll issues + comments (`since`),
and issue-events (last 100, filtered by time — the one activity-window limit).
Full mode = write migration/function, test on schema copy, deploy, verify,
commit, push. Anti-loop = skip the bot's own SIGNATURE-signed comments.

## 10. Safety model (per-source)
- Modes: `triage` (investigate + comment only) / `full` (act: fix/deploy for
  GitHub, status/assign/comment for Jetrix).
- Hard limits (never autonomous, any mode): security-posture changes (RLS/grants/
  auth), destructive/irreversible data ops, force-push, closing issues. → describe
  + defer to human.
- Danger-defer: judge CONSEQUENCE, not author. High-stakes/irreversible → post the
  ask + risk + recommend to the developer; err toward deferring when unsure.
- `@watcher` / `@claude` directive channel: any commenter can address the watcher
  to give a priority instruction for that ticket; dangerous asks still deferred.
- Auth-expiry detection (GitHub gh / Jetrix key) → retry, don't drop events,
  clear log hint to re-auth.
- Context authority: the target repo's own CLAUDE.md + project memory is the
  source of truth. The tool injects only behaviour rules, never a copy of project goals.

## 11. Interactive TUI + cooperative approval ("booyah" feature)
Requires flipping from "launchd fires a script" to a **daemon + attachable
client** (like tmux): a long-lived daemon holds queue + sessions; `watcher
attach` opens the TUI; the scheduler just keeps the daemon alive.
- TUI panes: queue • live session stream (tool calls + messages) • command bar.
- Slash/one-word commands (engine/queue level — easy, no mid-thought interrupt):
  `/queue /status /mode /pause /resume /skip /retry /memory /open /help`.
- Timeline view (received → investigated → asked → fixed → commented) + "next
  poll in Ns / idle / alive".
- **Cooperative approval flow (the agreed interaction):** at a natural decision/
  approval point the session pauses on the CURRENT event (safe — serial), prints
  a terminal prompt "Need clarification/approval — chat? (y/n)", waits ~10s.
  No answer → proceeds autonomously and records the action in the task/comment.
  `y` → waits for your typed message, feeds it into the session (resume with the
  answer as the next turn — same as pulling ticket detail), then continues.
  Only appears when `watcher attach` is open; background mode stays silent-
  autonomous (no unanswered prompts pile up).
- Mid-thought interruption (typing to stop Claude while it's thinking) is a
  SEPARATE, harder capability (needs streaming input) — DEFERRED. Between-event
  + cooperative-pause control covers ~90% at a fraction of the cost.

## 12. Session compaction (longevity)
Per source: every N events or daily, the session writes a short engineering-
memory summary, then the next event starts a FRESH session loading only that
summary + CLAUDE.md. Prevents slow/stale/expensive months-long sessions.

## 13. Actionable-scope decision — DEFERRED to pre-release
- Internal / Sihab: act on ALL issues (yours, Alif's, Nicolas's).
- Public / teammates: only assigned-to / mentions-me.
- Implemented as a per-source config field `scope: all | mine`, left unset;
  default locked deliberately right before any release. Admin visibility ≠
  "act on all" — scope filter by email applies even under Jetrix admin-bypass.

## 14. Migration (no disruption)
Current Clear GitHub watcher keeps running on the old single-project setup while
the new engine is built. When ready: migrate it in as the first registered
source (state/session preserved), repoint the scheduler, retire old paths —
verified before switching.

## 15. Build sequence
1. Engine refactor: core + adapters + per-source state + queue (works headless,
   nobody watching). Generic GitHubAdapter = today's logic.
2. `watcher` CLI + cross-OS installer + scheduling.
3. JetrixAdapter against §8 (test on dev-jetrix sandbox, then real data).
4. Daemon + `watcher attach` TUI: command bar, slash commands, timeline,
   cooperative y/n approval flow.

## 16. Explicitly deferred / not building now
Multi-agent runtime abstraction (Claude/Codex/Gemini) — skip, no value today.
Event bus (Slack/CI/Sentry/PagerDuty/…) — adapter seam already open; don't build
infra speculatively. Inbox (triage-don't-wake) — queue already is a light inbox;
defer to high volume. Git worktrees — unneeded while FIFO-serial (no concurrent
edits). Rename/positioning — later. Mid-thought interrupt — later. Priority
queue (P0–P4) — FIFO fine at current volume.

## 17. Known limits (not hidden)
Polling latency = poll interval (~2 min recommended); not instant (no laptop
webhooks). GitHub activity API: last-100-events window, very long downtime on a
busy repo could drop oldest activity events (issues/comments unaffected). Runs
only while the machine is awake; catches up on wake.
