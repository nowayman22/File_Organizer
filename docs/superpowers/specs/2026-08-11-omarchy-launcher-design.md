# File Organizer — Omarchy App Launcher — Design

**Date:** 2026-08-11
**Status:** Approved by user
**Base:** `File_Organizer.py` (formerly `Organizer_Linux.py`), post re-brand

## Goal

Make File Organizer launchable "like an app" on Omarchy — i.e. it appears in
Walker (the Omarchy app launcher, Super+Space) with a proper icon, and opens on
launch. Deliver the launcher files **inside the repo** with an installer so the
setup is portable and versioned.

## Context (from exploration)

- The old project shipped a one-time `~/.local/share/applications/photo-organizer.desktop`
  pointing at the old path `/home/nikits/Projects/Organizer/Organizer_Linux.py`, with
  `Name=Photo Organizer` and `StartupWMClass=Photoorganizer`. It is now stale:
  wrong path, wrong name, and the WM class doesn't even match the app.
- Omarchy launcher is Walker (`/usr/bin/walker`), which indexes `.desktop` files in
  `~/.local/share/applications/`.
- Icon theme is `Yaru-olive` (Yaru variants installed).
- The app sets `tk.Tk(className="FileOrganizer")` in `main()` → WM_CLASS class is
  `FileOrganizer`. The new entry's `StartupWMClass` must be exactly `FileOrganizer`.
- Active Omarchy theme colors (`~/.config/omarchy/current/theme/colors.toml`, Gruvbox
  family): accent `#7daea3`, background `#282828`, foreground `#d4be98`.
- `desktop-file-validate` and `gio` are installed.

## Files to add (in repo)

### 1. `packaging/file-organizer.desktop` — template

Desktop entry with a `@PROJECT_DIR@` placeholder that the installer substitutes.

```ini
[Desktop Entry]
Type=Application
Name=File Organizer
Comment=Copy or move files of any type, sorted by date or type
Exec=python3 @PROJECT_DIR@/File_Organizer.py
Icon=file-organizer
Terminal=false
Categories=Utility;FileTools;
StartupWMClass=FileOrganizer
Keywords=files;organize;copy;move;sort;
```

- `Exec` uses `python3` (matches the old entry; no venv assumed).
- `Icon=file-organizer` is the icon name installed into the hicolor theme.

### 2. `packaging/file-organizer.svg` — custom icon

- Square SVG, 256×256, scalable.
- A folder with a document sheet, using the active Omarchy palette:
  folder fill `#7daea3` (accent) with a darker folder back `#3c3836` (color0);
  document sheet `#282828` (background) with text lines `#d4be98` (foreground).
- Icon name on disk: `file-organizer.svg` → installed to
  `~/.local/share/icons/hicolor/scalable/apps/file-organizer.svg`.

### 3. `packaging/install.sh` — installer (idempotent)

- Resolve project root: `PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"`.
- Substitute `@PROJECT_DIR@` in the template (`sed`) and write
  `~/.local/share/applications/file-organizer.desktop`.
- Copy the icon to `~/.local/share/icons/hicolor/scalable/apps/file-organizer.svg`.
- Remove the stale `~/.local/share/applications/photo-organizer.desktop`.
- Refresh caches when available:
  `update-desktop-database ~/.local/share/applications` and
  `gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor` (both guarded by
  `command -v`).
- `--uninstall` flag removes the entry and the icon (reverses the install).
- Prints what it did; exits nonzero with a message on failure.
- No sudo required — user-level install only.

## Git workflow

- Work continues on branch `feature/file-organizer` (PR #1 is still open).
  These commits update PR #1 so the launcher lands together with the File
  Organizer change.

## Verification

- `desktop-file-validate ~/.local/share/applications/file-organizer.desktop`
  → no output (valid).
- Icon file exists and is well-formed XML.
- `gio launch ~/.local/share/applications/file-organizer.desktop` opens the app
  (display permitting); `hyprctl clients | grep -i class` shows class
  `FileOrganizer` (matching `StartupWMClass`).
- Walker (Super+Space) shows "File Organizer".

## Testing

Testable units around the shell assets (all in `tests/test_organizer.py`, headless):

1. **Desktop template:** the `.desktop` template contains all required keys
   (`Type`, `Name`, `Exec`, `Icon`, `Terminal`, `Categories`, `StartupWMClass`),
   the `@PROJECT_DIR@` placeholder, `StartupWMClass=FileOrganizer`, and that
   `FileOrganizer` matches the `className` in `File_Organizer.py` (`main()`).
2. **SVG icon:** `packaging/file-organizer.svg` parses as well-formed XML and
   has the right root element/dimensions.
3. **Installer:** run `install.sh` with `HOME` pointed at a temp dir → the
   `.desktop` lands in `$HOME/.local/share/applications/` with `@PROJECT_DIR@`
   substituted to the resolved project path, the icon lands in
   `$HOME/.local/share/icons/hicolor/scalable/apps/`, and a pre-existing stale
   `photo-organizer.desktop` in the fake home is removed. `--uninstall` removes
   both installed files.

## Out of scope

- Hyprland keybinds / Omarchy hooks or extensions.
- System-wide (root) install.
- Auto-restyling the icon when the Omarchy theme changes.
