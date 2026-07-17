#!/usr/bin/env bash
# Watcher installer (macOS / Linux). Installs the `watcher` command.
#   curl -fsSL <url>/install.sh | bash
set -euo pipefail

REPO="${WATCHER_REPO:-https://github.com/SHIHAB69/clear-issue-watcher.git}"
DEST="${WATCHER_DIR:-$HOME/tools/clear-issue-watcher}"

echo "▶ Watcher installer"
command -v python3 >/dev/null || { echo "✗ python3 required"; exit 1; }
command -v git >/dev/null || { echo "✗ git required"; exit 1; }

if [ -d "$DEST/.git" ]; then
  echo "▶ Updating $DEST"; git -C "$DEST" pull --ff-only
else
  echo "▶ Cloning into $DEST"; mkdir -p "$(dirname "$DEST")"; git clone "$REPO" "$DEST"
fi

echo "▶ Installing the watcher command (pip --user)"
python3 -m pip install --user -e "$DEST"

BIN="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
echo
echo "✓ Installed. If 'watcher' isn't found, add this to your shell profile:"
echo "    export PATH=\"$BIN:\$PATH\""
echo
echo "Next:"
echo "  watcher doctor          # check prerequisites (needs: claude; gh for GitHub sources)"
echo "  cd <your-project> && watcher    # add a source (GitHub auto-detects; or pick Jetrix)"
echo "  watcher start           # run it in the background"
