#!/bin/bash
# Record the README GIFs from a fake board and write them to docs/. macOS; needs agg and ffmpeg
# (brew install agg ffmpeg), gifsicle optional. Usage: dev/demo/make.sh [cli] [fanout] [ui]
set -euo pipefail
cd "$(dirname "$0")"
OUT="${TMPDIR:-/tmp}/nr-demo-out"
DOCS="$(cd ../.. && pwd)/docs"
# Tokyo Night, to match the UI's default theme: background, foreground, then the 16 ANSI colours.
TN="1a1b26,c0caf5,15161e,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,a9b1d6,414868,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,c0caf5"
python3 scenes.py "$OUT" >/dev/null
for scene in ${@:-cli fanout ui}; do
  ./setup.sh "$scene" >/dev/null
  python3 record.py "$OUT/$scene.json" "$OUT/$scene.cast"
  agg -q --theme "$TN" --font-size 16 --last-frame-duration 4 "$OUT/$scene.cast" "$OUT/$scene.gif"
  if command -v gifsicle >/dev/null; then
    gifsicle -w -O3 --colors 256 --lossy=30 "$OUT/$scene.gif" -o "$DOCS/$scene.gif"
  else
    cp "$OUT/$scene.gif" "$DOCS/$scene.gif"
  fi
  echo "docs/$scene.gif  $(du -h "$DOCS/$scene.gif" | cut -f1)"
done
rm -rf "${DEMO_HOME:-/private/tmp/nr-demo}"
