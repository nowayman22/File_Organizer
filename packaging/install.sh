#!/usr/bin/env bash
# Install / uninstall File Organizer as a desktop app (Omarchy / Walker).
# Usage: install.sh [--uninstall]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

DESKTOP_DEST="$APPS_DIR/file-organizer.desktop"
ICON_DEST="$ICON_DIR/file-organizer.svg"
STALE_ENTRY="$APPS_DIR/photo-organizer.desktop"

refresh_caches() {
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
}

install() {
    mkdir -p "$APPS_DIR" "$ICON_DIR"
    sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
        "$SCRIPT_DIR/file-organizer.desktop" > "$DESKTOP_DEST"
    cp "$SCRIPT_DIR/file-organizer.svg" "$ICON_DEST"
    rm -f "$STALE_ENTRY"
    refresh_caches
    echo "Installed File Organizer launcher."
    echo "  desktop : $DESKTOP_DEST"
    echo "  icon    : $ICON_DEST"
    echo "Open Walker (Super+Space) and search 'File Organizer'."
}

uninstall() {
    rm -f "$DESKTOP_DEST" "$ICON_DEST"
    refresh_caches
    echo "File Organizer launcher removed."
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
else
    install
fi
