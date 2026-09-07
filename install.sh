#!/usr/bin/env bash
# Installe claudini-console : les deux commandes shell et l'app menu bar.
# Tout est posé en lien symbolique vers ce dépôt — un `git pull` suffit ensuite.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$HOME/.claudini/tools"
BIN="$HOME/.local/bin"
APP="$HOME/Applications/ClaudiniBar.app"

command -v claudini >/dev/null || {
  echo "claudini est requis : https://github.com/kimrgrey/claudini" >&2
  exit 1
}

mkdir -p "$TOOLS" "$BIN"
ln -sfn "$REPO/src/claudini_usage.py" "$TOOLS/claudini_usage.py"
ln -sfn "$REPO/src/claudini_auto.py"  "$TOOLS/claudini_auto.py"
ln -sfn "$REPO/src/claudini_usage.py" "$BIN/claudini-usage"
ln -sfn "$REPO/src/claudini_auto.py"  "$BIN/claudini-auto"
echo "commandes  -> $BIN/claudini-usage, $BIN/claudini-auto"

if command -v swiftc >/dev/null; then
  BUILD="$(mktemp -d)"
  swiftc -O -o "$BUILD/ClaudiniBar" "$REPO/menubar/main.swift" -framework AppKit
  pkill -f "ClaudiniBar.app" 2>/dev/null || true
  mkdir -p "$APP/Contents/MacOS"
  cp "$BUILD/ClaudiniBar" "$APP/Contents/MacOS/ClaudiniBar"
  cp "$REPO/menubar/Info.plist" "$APP/Contents/Info.plist"
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
  rm -rf "$BUILD"
  echo "menu bar   -> $APP"
  open "$APP"
else
  echo "swiftc absent (Xcode Command Line Tools) — app menu bar non construite"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "ajoute $BIN à ton PATH" ;;
esac
