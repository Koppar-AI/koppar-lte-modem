#!/bin/sh
# Build Koppar Phone.app, the menu-bar app.
#   ./build.sh                       -> "build/Koppar Phone.app", using ../.venv/bin/koppar-lte-modem
# Environment:
#   KOPPAR_EMBED_CLI      a standalone CLI folder (packaging/build-cli.sh: dist/koppar-lte-modem)
#                         to put inside the app; release builds do, so the app needs nothing else
#   KOPPAR_CLI            else: the koppar-lte-modem command the app runs (a path on this computer)
#   KOPPAR_VERSION        version shown in About (default 0.1.0)
#   KOPPAR_ARCH           arm64 or x86_64 (default: this Mac's)
#   KOPPAR_SIGN_IDENTITY  "Developer ID Application: ..." to sign for distribution
#                         (hardened runtime); default "-" signs ad hoc, for this Mac
set -eu
cd "$(dirname "$0")"
VERSION="${KOPPAR_VERSION:-0.1.0}"
ARCH="${KOPPAR_ARCH:-$(uname -m)}"
IDENTITY="${KOPPAR_SIGN_IDENTITY:--}"
APP="build/Koppar Phone.app"
rm -rf "$APP" build/AppIcon.iconset
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/AppIcon.iconset

xcrun swiftc -O -swift-version 5 -parse-as-library -target "$ARCH-apple-macos13.0" \
    Koppar/main.swift -o "$APP/Contents/MacOS/Koppar"

# The icon, from icon.svg (needs rsvg-convert: brew install librsvg).
for s in 16 32 128 256 512; do
    rsvg-convert -w $s -h $s Koppar/icon.svg -o build/AppIcon.iconset/icon_${s}x${s}.png
    rsvg-convert -w $((s * 2)) -h $((s * 2)) Koppar/icon.svg -o build/AppIcon.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

if [ -n "${KOPPAR_EMBED_CLI:-}" ]; then
    cp -R "$KOPPAR_EMBED_CLI" "$APP/Contents/Resources/cli"
    CLI="@bundle/cli/koppar-lte-modem"
else
    CLI="${KOPPAR_CLI:-$(cd .. && pwd)/.venv/bin/koppar-lte-modem}"
fi
sed -e "s#@KOPPAR_CLI@#$CLI#" -e "s#<string>0.1.0</string>#<string>$VERSION</string>#" \
    Koppar/Info.plist > "$APP/Contents/Info.plist"

# Sign every binary inside first (the embedded CLI), then the app.
if [ "$IDENTITY" = "-" ]; then
    set -- --force --sign -
else
    set -- --force --sign "$IDENTITY" --options runtime --timestamp --entitlements Koppar/Koppar.entitlements
fi
if [ -d "$APP/Contents/Resources/cli" ]; then
    find "$APP/Contents/Resources/cli" -type f | while read -r f; do
        if file -b "$f" | grep -q Mach-O; then codesign "$@" "$f"; fi
    done
fi
codesign "$@" "$APP"
codesign --verify --strict "$APP"
echo "built $APP ($VERSION, $ARCH; daemon: $CLI)"
