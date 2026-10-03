#!/usr/bin/env bash
# Build a hearth_<version>_all.deb from the repo.
#   ./packaging/deb/build-deb.sh [output-dir]
# Output dir defaults to the repo root. Needs: dpkg-deb (dpkg-dev).
set -Eeuo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"   # packaging/deb
REPO="$(cd "$HERE/../.." && pwd)"                          # repo root
OUT="$(cd "${1:-$REPO}" && pwd)"

VERSION="$(python3 - "$REPO/hearth/__init__.py" <<'PY'
import re, sys
print(re.search(r'VERSION\s*=\s*"([^"]+)"', open(sys.argv[1]).read()).group(1))
PY
)"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
PKG="$BUILD/hearth"

# --- program files -> /usr/share/hearth ---
install -d "$PKG/usr/share/hearth/hearth" "$PKG/usr/share/hearth/web"
cp -r "$REPO/hearth/." "$PKG/usr/share/hearth/hearth/"
cp -r "$REPO/web/."    "$PKG/usr/share/hearth/web/"
find "$PKG/usr/share/hearth" -name __pycache__ -type d -prune -exec rm -rf {} +

# --- launcher -> /usr/bin/hearth ---
install -d "$PKG/usr/bin"
install -m0755 "$HERE/hearth-launcher" "$PKG/usr/bin/hearth"

# --- systemd user unit -> /usr/lib/systemd/user ---
install -d "$PKG/usr/lib/systemd/user"
install -m0644 "$HERE/hearth.service" "$PKG/usr/lib/systemd/user/hearth.service"

# --- desktop entry + icon ---
install -d "$PKG/usr/share/applications"
install -m0644 "$HERE/org.homelab.Hearth.desktop" "$PKG/usr/share/applications/org.homelab.Hearth.desktop"
install -d "$PKG/usr/share/icons/hicolor/scalable/apps"
install -m0644 "$REPO/web/hearth.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/org.homelab.Hearth.svg"

# --- documentation: copyright + changelog (required by Debian policy) ---
install -d "$PKG/usr/share/doc/hearth"
install -m0644 "$HERE/copyright" "$PKG/usr/share/doc/hearth/copyright"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$(date -R)/" "$HERE/changelog" \
    | gzip -9n > "$PKG/usr/share/doc/hearth/changelog.gz"

# --- normalise permissions (dirs 0755, files 0644) before setting executables ---
find "$PKG/usr" -type d -exec chmod 0755 {} +
find "$PKG/usr" -type f -exec chmod 0644 {} +
chmod 0755 "$PKG/usr/bin/hearth"

# --- control + maintainer scripts ---
install -d -m0755 "$PKG/DEBIAN"
sed "s/@VERSION@/$VERSION/" "$HERE/control" > "$PKG/DEBIAN/control"
chmod 0644 "$PKG/DEBIAN/control"
for s in postinst prerm postrm; do install -m0755 "$HERE/$s" "$PKG/DEBIAN/$s"; done

DEB="$OUT/hearth_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$PKG" "$DEB"
echo "Built: $DEB"
