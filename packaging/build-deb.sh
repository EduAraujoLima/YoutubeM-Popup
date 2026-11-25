#!/usr/bin/env bash
# Gera dist/ytm-popup_<versão>_all.deb
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
PKG="$ROOT/build/ytm-popup_${VERSION}_all"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/ytm-popup" \
         "$PKG/usr/share/applications" "$PKG/etc/xdg/autostart" \
         "$PKG/usr/share/doc/ytm-popup"

install -m 755 "$ROOT/bin/ytm-popup"           "$PKG/usr/bin/ytm-popup"
install -m 755 "$ROOT/bin/ytm-popup-shortcut"  "$PKG/usr/bin/ytm-popup-shortcut"
ln -s /usr/bin/ytm-popup-shortcut              "$PKG/usr/share/ytm-popup/ytm-popup-shortcut"
install -m 644 "$ROOT/src/ytm_popup.py"        "$PKG/usr/share/ytm-popup/ytm_popup.py"
install -m 644 "$ROOT/data/ytm-popup.desktop"  "$PKG/usr/share/applications/"
install -m 644 "$ROOT/data/ytm-popup-setup.desktop" "$PKG/etc/xdg/autostart/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$PKG/usr/share/doc/ytm-popup/"

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: ytm-popup
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, python3-venv, mpv, dconf-cli
Recommends: zenity, libnotify-bin, firefox | firefox-esr
Installed-Size: $SIZE
Maintainer: EduAraujoLima <EduAraujoLima@users.noreply.github.com>
Homepage: https://github.com/EduAraujoLima/YoutubeM-Popup
Description: Mini player popup do YouTube Music com atalho de teclado
 Popup pequeno (Super+M) com capa do álbum, controles de reprodução,
 volume e busca. Usa automaticamente o login do YouTube do Firefox
 (inclusive Premium) se existir; caso contrário toca em modo anônimo.
 Feito para o Linux Mint MATE.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
  echo ""
  echo "ytm-popup instalado. Abra 'YT Music Popup' no menu uma vez"
  echo "(ou saia e entre na sessão) para ativar o atalho Super+M."
fi
EOF
cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "purge" ] || [ "$1" = "remove" ]; then
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
fi
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$PKG" "$ROOT/dist/ytm-popup_${VERSION}_all.deb"
echo "OK: dist/ytm-popup_${VERSION}_all.deb"
