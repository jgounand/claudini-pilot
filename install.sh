#!/usr/bin/env bash
# Installs claudini-pilot: the two commands, linked back to this clone so a
# `git pull` updates them, and ClaudiniBar with its widget.
#
#     ./install.sh                  install or update
#     ./install.sh --at-login       also start ClaudiniBar at login
#     ./install.sh --not-at-login   and undo that
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# /Applications, because that is the folder Finder's sidebar calls
# "Applications"; ~/Applications is a different one you have to navigate to.
APP="/Applications/ClaudiniBar.app"
[ -w /Applications ] || APP="$HOME/Applications/ClaudiniBar.app"

# A menu bar app that does not come back after a restart is a menu bar app you
# stop trusting. Opt-in, and removable from System Settings > General >
# Login Items like anything else.
if [ "${1:-}" = "--at-login" ]; then
  osascript -e "tell application \"System Events\" to make login item at end \
                with properties {path:\"$APP\", hidden:true}" >/dev/null
  echo "will start at login"
elif [ "${1:-}" = "--not-at-login" ]; then
  osascript -e 'tell application "System Events" to delete login item "ClaudiniBar"' \
    2>/dev/null || true
  echo "will no longer start at login"
fi
TOOLS="$HOME/.claudini/tools"
BIN="$HOME/.local/bin"

mkdir -p "$TOOLS" "$BIN"
ln -sfn "$REPO/src/claudini_usage.py" "$TOOLS/claudini_usage.py"
ln -sfn "$REPO/src/claudini_auto.py"  "$TOOLS/claudini_auto.py"
ln -sfn "$REPO/src/claudini_history.py" "$TOOLS/claudini_history.py"
ln -sfn "$REPO/src/claudini_usage.py" "$BIN/claudini-usage"
ln -sfn "$REPO/src/claudini_auto.py"  "$BIN/claudini-auto"
echo "commands   -> $BIN/claudini-usage, $BIN/claudini-auto"

# The app, with its widget when this Mac can sign one: that takes Xcode and an
# "Apple Development" certificate (free with any Apple ID, from Xcode >
# Settings > Accounts). The widget shares a folder with the app, and macOS only
# allows that between apps signed by the same team.
TEAM="$(security find-certificate -c "Apple Development" -p 2>/dev/null \
        | openssl x509 -noout -subject 2>/dev/null \
        | sed -n 's/.*OU *= *\([A-Z0-9]*\).*/\1/p' | head -1)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT      # a failed build used to leave it behind

built=""
if [ -n "$TEAM" ] && xcodebuild -version >/dev/null 2>&1; then
  if xcodebuild -project "$REPO/ClaudiniBar.xcodeproj" -scheme ClaudiniBar \
       -configuration Release -derivedDataPath "$BUILD" DEVELOPMENT_TEAM="$TEAM" \
       build >"$BUILD/build.log" 2>&1; then
    built="$BUILD/Build/Products/Release/ClaudiniBar.app"
  else
    grep -E "error:" "$BUILD/build.log" | sort -u | head -20
    echo "Xcode build failed — falling back to the menu bar app without its widget"
  fi
fi

if [ -z "$built" ] && command -v swiftc >/dev/null; then
  [ -n "$TEAM" ] || echo "note: no Apple Development certificate — building without the widget"
  mkdir -p "$BUILD/ClaudiniBar.app/Contents/MacOS" "$BUILD/ClaudiniBar.app/Contents/Resources"
  swiftc -O -o "$BUILD/ClaudiniBar.app/Contents/MacOS/ClaudiniBar" \
    "$REPO"/menubar/*.swift "$REPO"/shared/*.swift "$REPO/widget/WidgetViews.swift" \
    -framework AppKit -framework SwiftUI -framework WidgetKit
  # Xcode fills in the build settings; this build has none to fill in.
  sed -e 's/$(EXECUTABLE_NAME)/ClaudiniBar/; s/$(PRODUCT_BUNDLE_IDENTIFIER)/com.github.claudini-console.bar/' \
      -e 's/$(PRODUCT_NAME)/ClaudiniBar/; s/$(PRODUCT_BUNDLE_PACKAGE_TYPE)/APPL/' \
      -e 's/$(MARKETING_VERSION)/1.1/; s/$(CURRENT_PROJECT_VERSION)/1/' \
      -e 's/$(DEVELOPMENT_LANGUAGE)/en/; s/$(MACOSX_DEPLOYMENT_TARGET)/14.0/' \
      "$REPO/menubar/Info.plist" > "$BUILD/ClaudiniBar.app/Contents/Info.plist"
  plutil -remove ClaudiniAppGroup "$BUILD/ClaudiniBar.app/Contents/Info.plist" >/dev/null
  cp "$REPO/menubar/ClaudiniBar.icns" "$BUILD/ClaudiniBar.app/Contents/Resources/"
  codesign --force --sign - "$BUILD/ClaudiniBar.app" >/dev/null 2>&1 || true
  built="$BUILD/ClaudiniBar.app"
fi

if [ -n "$built" ]; then
  pkill -f "ClaudiniBar.app" 2>/dev/null || true
  rm -rf "$APP"
  ditto "$built" "$APP"
  # Building registers the copy in the build folder with macOS, widget and
  # all. Left behind, it lingers after the folder is gone: a second "Claude
  # usage" in the widget gallery, and a stale target for opening the app.
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -u "$built" >/dev/null 2>&1 || true
  pluginkit -r "$built/Contents/PlugIns/ClaudiniWidget.appex" >/dev/null 2>&1 || true
  touch "$APP"                     # nudge Finder to re-read the icon
  if [ -d "$APP/Contents/PlugIns/ClaudiniWidget.appex" ]; then
    echo "menu bar   -> $APP  (with the Claude usage widget)"
  else
    echo "menu bar   -> $APP"
  fi
  open "$APP"
else
  echo "neither Xcode nor swiftc found — menu bar app not built"
fi

if ! osascript -e 'tell application "System Events" to get the name of every login item' \
     2>/dev/null | grep -q ClaudiniBar; then
  echo "to start it at login:  ./install.sh --at-login"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "add $BIN to your PATH" ;;
esac
