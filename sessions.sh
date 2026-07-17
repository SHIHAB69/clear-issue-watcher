#!/bin/bash
# List recent watcher triage runs and how to open each as a real Claude Code
# session. Usage: scripts/issue-watcher/sessions.sh [n]
N="${1:-10}"
TSV="$HOME/.clear-issue-watcher/sessions.tsv"
echo "mode: $(cat "$HOME/.clear-issue-watcher/mode" 2>/dev/null || echo triage)"
echo
if [[ ! -f "$TSV" ]]; then echo "no triage sessions recorded yet"; exit 0; fi
echo "WHEN (UTC)                        ISSUE  KIND         SESSION"
tail -n "$N" "$TSV" | column -t -s $'\t'
echo
echo "open one in the full Claude Code UI:"
echo "  cd /Users/sihabhowlader/clear.server.fresh && claude --resume <SESSION>"
echo "live activity:  tail -f ~/.clear-issue-watcher/watcher.log"
