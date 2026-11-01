#!/bin/bash
# 打发布包：release/usage-widget-<version>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || echo 0.0.0)"
NAME="usage-widget-$VERSION"
DIST="$ROOT/release/$NAME"

[ -x "$ROOT/bin/usage-widget" ] || "$ROOT/scripts/build.sh"

rm -rf "$DIST"
mkdir -p "$DIST/bin" "$DIST/docs"

cp "$ROOT/bin/usage-widget"            "$DIST/bin/"
cp "$ROOT/src/usage-monitor.py"        "$DIST/bin/usage-monitor"
cp "$ROOT/scripts/usage-widget-data"   "$DIST/bin/usage-widget-data"
cp "$ROOT/scripts/usage-wallpaper"     "$DIST/bin/usage-wallpaper"
cp "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/README.md" "$ROOT/LICENSE" "$DIST/"
cp "$ROOT/docs/"*.md "$ROOT/docs/"*.json "$DIST/docs/"

chmod 755 "$DIST/bin/"* "$DIST/install.sh" "$DIST/uninstall.sh"

cd "$ROOT/release"
COPYFILE_DISABLE=1 tar -czf "$NAME.tar.gz" "$NAME"
shasum -a 256 "$NAME.tar.gz" | tee "$NAME.tar.gz.sha256"
echo "产出: $ROOT/release/$NAME.tar.gz"
