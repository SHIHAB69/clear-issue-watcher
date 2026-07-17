# Clear issue watcher

Local automation that watches GitHub issues on `nicorogers/clear.server` and
runs **headless Claude Code** to triage — and optionally fix — them like a
senior engineer, following the same rules Shihab uses interactively.

It polls the repo on a timer (GitHub can't push to a laptop), queues each new
issue / comment / activity, and hands them **one at a time** to a single
persistent Claude Code session that keeps full context across all tickets.

---

## Install (once)

```bash
# 1. clone next to the server repo (paths in the scripts assume this layout)
git clone git@github.com:SHIHAB69/clear-issue-watcher.git ~/tools/clear-issue-watcher

# 2. install the launchd job (edit paths in the template first if yours differ)
cp ~/tools/clear-issue-watcher/com.clear.issue-watcher.plist.template \
   ~/Library/LaunchAgents/com.clear.issue-watcher.plist
#   -> point ProgramArguments at watch.py's real location

# 3. prerequisites: `gh auth login` (SHIHAB69), `claude` on PATH, repo checkout
#    at ~/clear.server.fresh with a working .env (SUPABASE_DB_URL etc.)
```

## Start / stop

```bash
# start (and restart after editing the plist)
launchctl unload ~/Library/LaunchAgents/com.clear.issue-watcher.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.clear.issue-watcher.plist

# stop
launchctl unload ~/Library/LaunchAgents/com.clear.issue-watcher.plist

# is it loaded?
launchctl list | grep clear          # a PID in col 1 = running

# run one cycle by hand (foreground, for debugging)
python3 ~/tools/clear-issue-watcher/watch.py
```

## Watch it work

```bash
# live, pretty stream of the active session (tool calls + messages) — best view
python3 ~/tools/clear-issue-watcher/monitor.py

# raw one-line event log
tail -f ~/.clear-issue-watcher/watcher.log

# list past runs + their session IDs
~/tools/clear-issue-watcher/sessions.sh

# replay any past run in the FULL interactive Claude Code UI
cd ~/clear.server.fresh && claude --resume <SESSION-ID>
```

## Modes

```bash
cat ~/.clear-issue-watcher/mode                 # current: triage | full
echo triage > ~/.clear-issue-watcher/mode       # comment-only (safe)
echo full   > ~/.clear-issue-watcher/mode        # fix + deploy + push autonomously
```
- **triage**: investigate and post ONE comment per event (fix *plans*, no writes).
- **full**: for server-fixable bugs it writes the migration/function, tests on a
  schema copy, deploys, verifies, commits and pushes. Hard limits always apply:
  no RLS/security-posture changes, no destructive data ops, no closing issues.

## Poll interval

Edit `StartInterval` (seconds) in the plist, then reload:
```bash
# e.g. 120s for steady state; 60s while testing
sed -i '' 's|<integer>[0-9]*</integer>|<integer>120</integer>|' \
  ~/Library/LaunchAgents/com.clear.issue-watcher.plist
```

## Test it

1. `python3 monitor.py` in one terminal.
2. Comment on any issue (your own account is fine — see anti-loop below).
3. Within one poll interval it queues and handles the event live.

---

## What triggers a run

| Activity | Fires? |
|---|---|
| New issue (any author, incl. your test issues) | yes |
| New comment by anyone | yes — except the bot's own SIGNATURE-signed comments |
| assigned / labeled / closed / reopened / renamed | yes — unless the actor is `SHIHAB69` |

**Anti-loop:** the bot acts as the `SHIHAB69` token. Comments are told apart by
the signature line, so *your* comments fire but the bot's don't. Assignment/
label/close events carry no signature and share the token, so all
`SHIHAB69`-actor events are skipped to avoid loops. Test with **comments**.
Nicolas's and Alif's activity of every kind fires.

## Files & state

| Path | What |
|---|---|
| `watch.py` | poller + FIFO queue + session runner |
| `monitor.py` | live terminal viewer |
| `sessions.sh` | list past runs / how to resume |
| `triage-prompt.md` | the architect brief given to the session |
| `~/.clear-issue-watcher/state.json` | `last_checked` + processed keys |
| `~/.clear-issue-watcher/queue.jsonl` | pending events (FIFO) |
| `~/.clear-issue-watcher/session` | id of the rolling session to resume |
| `~/.clear-issue-watcher/sessions.tsv` | history of runs |
| `~/.clear-issue-watcher/watcher.log` | event log |
| `~/.clear-issue-watcher/mode` | `triage` or `full` |

---

## Offline / asleep / stopped — what happens to missed activity

**Nothing is lost for issues and comments.** The watcher persists a
`last_checked` timestamp and, on its next run, asks GitHub for everything
created **since** then. So:

- **Laptop asleep (lid closed) / powered off:** `launchd` can't fire while the
  machine is asleep; on wake it runs the job at the next opportunity. Because
  `last_checked` is persisted, that first post-wake cycle catches up every
  issue and comment created during the gap and queues them in order.
- **Offline (no network):** the `gh` calls fail, the cycle logs an error and
  exits **without advancing `last_checked`**, so the next cycle simply retries
  the same window. No skipped events, no double-processing (the `processed`
  set also dedupes).
- **Tool stopped for hours/days:** same as asleep — one catch-up cycle drains
  the whole backlog through the queue, one event at a time.
- **Mid-event crash / timeout:** the event stays at the head of the queue and
  is retried next cycle (it's popped only on success). Duplicate comments are
  prevented by the `processed` keys.

**The one real gap:** the *activity* stream (assign/label/close) uses GitHub's
issue-events API, which has no "since" filter — the watcher reads the most
recent 100 events and filters by time. If the tool is off long enough that
**more than 100 assign/label/close events** accumulate, the oldest ones beyond
that window are missed. New issues and comments are unaffected (their APIs do
support `since`). In practice, for laptop sleep or a normal outage this never
triggers; it would only matter after very long downtime on a very busy repo. If
that becomes real, switch the activity poll to the per-issue timeline API or
shorten downtime.

To see what it did while you were away: `~/tools/clear-issue-watcher/sessions.sh`
and the tail of `watcher.log`.
