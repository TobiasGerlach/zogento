#!/usr/bin/env bash
# Verpackt dist/Bibliothek/ (PyInstaller-Ordner) als .deb für Ubuntu/Debian.
#   /opt/bibliothek/            Programm
#   /usr/bin/bib, bibliothek    Symlinks
#   Startmenü-Eintrag + Icon
# Aufruf: packaging/linux/make_deb.sh <version>
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION="${1:?Version fehlt}"
ARCH="$(dpkg --print-architecture)"
MAINTAINER="${DEB_MAINTAINER:-Tobias Gerlach <bibliothek@localhost>}"
PKG="build/deb/bibliothek_${VERSION}_${ARCH}"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/icons/hicolor/512x512/apps"
cp -a dist/Bibliothek "$PKG/opt/bibliothek"
ln -s /opt/bibliothek/bib "$PKG/usr/bin/bib"
ln -s /opt/bibliothek/Bibliothek "$PKG/usr/bin/bibliothek"
install -m 644 packaging/linux/bibliothek.desktop "$PKG/usr/share/applications/bibliothek.desktop"
.venv/bin/python - "$PKG" <<'PY'
import sys
from PIL import Image
pkg = sys.argv[1]
img = Image.open("packaging/assets/icon.png")
for size in (256, 512):
    img.resize((size, size), Image.LANCZOS).save(f"{pkg}/usr/share/icons/hicolor/{size}x{size}/apps/bibliothek.png")
PY

SIZE_KB="$(du -sk "$PKG" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: bibliothek
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE_KB
Depends: tesseract-ocr, tesseract-ocr-deu, tesseract-ocr-eng, libc6 (>= 2.35)
Recommends: xdg-utils
Maintainer: $MAINTAINER
Description: Zentrale Literaturverwaltung mit OCR und KI-tauglicher Suche
 Verwaltet PDFs, Word-Dateien, EPUBs und Buchscans an einem Ort, erkennt
 Text per OCR, sucht im Volltext und semantisch und gibt Fundstellen mit
 Seitenangabe aus. Kommandozeile: bib. Oberfläche: Bibliothek.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
POST
chmod 755 "$PKG/DEBIAN/postinst"

mkdir -p dist
dpkg-deb --build --root-owner-group -Zxz "$PKG" "dist/bibliothek_${VERSION}_${ARCH}.deb"
du -h "dist/bibliothek_${VERSION}_${ARCH}.deb"
