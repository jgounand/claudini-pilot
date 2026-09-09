#!/usr/bin/env bash
# Regenerate ClaudiniBar.icns from icon.html. Only needed when the design
# changes — the result is committed so installing needs no browser.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || { echo "needs Google Chrome to render the artwork" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
"$CHROME" --headless --disable-gpu --hide-scrollbars \
  --default-background-color=00000000 --window-size=1024,1024 \
  --screenshot="$WORK/icon.png" "file://$PWD/icon.html" 2>/dev/null

mkdir -p "$WORK/ClaudiniBar.iconset"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$WORK/icon.png" \
       --out "$WORK/ClaudiniBar.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$WORK/icon.png" \
       --out "$WORK/ClaudiniBar.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/ClaudiniBar.iconset" -o ClaudiniBar.icns
echo "wrote $PWD/ClaudiniBar.icns"
