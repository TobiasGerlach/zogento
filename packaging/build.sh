#!/usr/bin/env bash
# Baut das App-Paket für das laufende System:
#   macOS -> dist/Bibliothek-<version>-macos-arm64.dmg
#   Linux -> dist/bibliothek_<version>_amd64.deb
#
# Aufruf aus beliebigem Ordner:  packaging/build.sh
#   SKIP_SYNC=1   Umgebung nicht neu synchronisieren (wenn sie schon steht)
#   BIBLIOTHEK_VERSION=1.2.3   Version vorgeben (sonst aus pyproject.toml)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

if [ "${SKIP_SYNC:-}" != "1" ]; then
  uv sync --extra build
fi
PY="$ROOT/.venv/bin/python"

VERSION="${BIBLIOTHEK_VERSION:-$("$PY" -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')}"
export BIBLIOTHEK_VERSION="$VERSION"
echo "== Bibliothek $VERSION für $(uname -s) =="

"$PY" packaging/make_icon.py
rm -rf build/pyinstaller dist/Bibliothek dist/Bibliothek.app
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller packaging/bibliothek.spec

case "$(uname -s)" in
  Darwin)
    APP="dist/Bibliothek.app"
    BIB="$APP/Contents/MacOS/bib"
    echo "== Selbsttest =="
    BIBLIOTHEK_SMOKE=1 "$BIB"
    echo "== Ad-hoc-Signatur =="
    codesign --force --deep --sign - "$APP"
    codesign --verify --deep --strict "$APP"
    echo "== DMG =="
    STAGE="build/dmg"
    rm -rf "$STAGE" && mkdir -p "$STAGE"
    cp -R "$APP" "$STAGE/"
    ln -s /Applications "$STAGE/Programme"
    DMG="dist/Bibliothek-$VERSION-macos-$(uname -m).dmg"
    rm -f "$DMG"
    hdiutil create -volname "Bibliothek $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
    rm -rf "$STAGE"
    du -h "$DMG"
    ;;
  Linux)
    echo "== Selbsttest =="
    BIBLIOTHEK_SMOKE=1 dist/Bibliothek/bib
    packaging/linux/make_deb.sh "$VERSION"
    ;;
  *)
    echo "Unter Windows packaging/build.ps1 verwenden." >&2
    exit 1
    ;;
esac
