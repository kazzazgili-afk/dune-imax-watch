#!/usr/bin/env bash
# Installs and loads the launchd agent for dune-watch on macOS.
# Review deploy/com.gilikazzaz.dune-watch.plist before running - it hardcodes an
# absolute path to this checkout's .venv and config.yaml.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.gilikazzaz.dune-watch.plist"
PLIST_SRC="$PROJECT_DIR/deploy/$PLIST_NAME"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

if [ ! -f "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "No .venv found at $PROJECT_DIR/.venv - create it first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [ ! -f "$PROJECT_DIR/config/config.yaml" ]; then
    echo "No config/config.yaml found - copy config/config.example.yaml first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/Logs/dune-watch"
cp "$PLIST_SRC" "$PLIST_DEST"

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
launchctl start com.gilikazzaz.dune-watch

echo "Installed and started com.gilikazzaz.dune-watch."
echo "Logs: $HOME/Library/Logs/dune-watch/"
echo "To stop:    launchctl unload $PLIST_DEST"
