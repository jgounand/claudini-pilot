#!/usr/bin/env bash
# The icon is committed rather than drawn here: menubar/make-icon.py is its
# source, and regenerates it when the design changes.
# Installe claudini-console : les deux commandes shell et l'app menu bar.
# Tout est posé en lien symbolique vers ce dépôt — un `git pull` suffit ensuite.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$HOME/Applications/ClaudiniBar.app"

# A menu bar app that does not come back after a restart is a menu bar app you
# stop trusting. Opt-in, and removable from System Settings > General >
# Login Items like anything else.
if [ "${1:-}" = "--at-login" ]; then
  osascript -e "tell application \"System Events\" to make login item at end \
                with properties {path:\"$APP_PATH\", hidden:true}" >/dev/null
  echo "will start at login"
elif [ "${1:-}" = "--not-at-login" ]; then
  osascript -e 'tell application "System Events" to delete login item "ClaudiniBar"' \
    2>/dev/null || true
  echo "will no longer start at login"
fi
TOOLS="$HOME/.claudini/tools"
BIN="$HOME/.local/bin"
APP="$HOME/Applications/ClaudiniBar.app"

# claudini is no longer required — this manages profiles, switching and logins
# on its own — but if you already use it, the two read the same layout.
command -v claudini >/dev/null || echo "note: claudini not found; not needed."

mkdir -p "$TOOLS" "$BIN"
ln -sfn "$REPO/src/claudini_usage.py" "$TOOLS/claudini_usage.py"
ln -sfn "$REPO/src/claudini_auto.py"  "$TOOLS/claudini_auto.py"
ln -sfn "$REPO/src/claudini_history.py" "$TOOLS/claudini_history.py"
ln -sfn "$REPO/src/claudini_usage.py" "$BIN/claudini-usage"
ln -sfn "$REPO/src/claudini_auto.py"  "$BIN/claudini-auto"
echo "commandes  -> $BIN/claudini-usage, $BIN/claudini-auto"

if command -v swiftc >/dev/null; then
  BUILD="$(mktemp -d)"
  trap 'rm -rf "$BUILD"' EXIT      # a failed compile used to leave it behind
  swiftc -O -o "$BUILD/ClaudiniBar" "$REPO/menubar/main.swift" -framework AppKit
  pkill -f "ClaudiniBar.app" 2>/dev/null || true
  mkdir -p "$APP/Contents/MacOS"
  cp "$BUILD/ClaudiniBar" "$APP/Contents/MacOS/ClaudiniBar"
  cp "$REPO/menubar/Info.plist" "$APP/Contents/Info.plist"
  mkdir -p "$APP/Contents/Resources"
  cp "$REPO/menubar/ClaudiniBar.icns" "$APP/Contents/Resources/"
  touch "$APP"                     # nudge Finder to re-read the icon
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
  echo "menu bar   -> $APP"
  open "$APP"
else
  echo "swiftc absent (Xcode Command Line Tools) — app menu bar non construite"
fi

if ! osascript -e 'tell application "System Events" to get the name of every login item' \
     2>/dev/null | grep -q ClaudiniBar; then
  echo "to start it at login:  ./install.sh --at-login"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "ajoute $BIN à ton PATH" ;;
esac
