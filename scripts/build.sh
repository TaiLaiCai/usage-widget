#!/bin/bash
# 编译通用二进制（x86_64 + arm64）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/bin"
SRC="$ROOT/src/usage-widget.swift"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "需要 swiftc：xcode-select --install" >&2
  exit 1
fi

for arch in arm64 x86_64; do
  echo "编译 $arch ..."
  swiftc -O -target "$arch-apple-macos13.0" "$SRC" -framework AppKit -o "$TMP/$arch"
done

lipo -create -output "$OUT/usage-widget" "$TMP/arm64" "$TMP/x86_64"
chmod 755 "$OUT/usage-widget"
codesign --force --sign - "$OUT/usage-widget" >/dev/null 2>&1 || true

echo "产出: $OUT/usage-widget"
lipo -info "$OUT/usage-widget"
