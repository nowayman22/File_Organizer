# Tabbed UI and In-App Settings — Design

**Date:** 2026-11-03
**Status:** Implemented
**Base:** `File_Organizer.py` (after the Google Drive feature)

## Goal

Split the single crowded surface into three tabs — **Organize**, **Options**, **Config** — so
run-time choices, per-run presets and rarely-changed configuration each have a home, and so
configuration that used to require a terminal (rclone remotes) or a code edit (scanner skip
rules) can be changed in the app.

## Layout

| Tab | Contents | Why |
|---|---|---|
| **Organize** | source, destination, USB/Drive pickers, file types, Scan/Clear log, Start/Cancel, progress, log | the hot path: everything touched during a run stays visible |
| **Options** | profiles (select/save/save-as/delete), run mode, copy/move, sort mode, duplicates, double-check, after-transfer, logging toggles | per-run and per-profile choices, changed often but not mid-run |
| **Config** | Google Drive remotes (refresh/add/test/remove, default folder), scanner skip folders/files, save/reset | settings that are global and rarely change |

The log and progress stay on Organize (not a separate tab) so a run is still watchable
while the Options/Config tabs are open.

## Settings persistence

New file `~/.file_organizer/settings.json`, separate from `profiles.json`:

```json
{
  "skipped_dirs": ["node_modules", "venv", "__pycache__", "..."],
  "skipped_files": ["Thumbs.db", "desktop.ini", "ehthumbs.db"],
  "drive_folder": "FileOrganizer"
}
```

- Defaults come from the module constants (`SKIPPED_DIRS`, `SKIPPED_FILES`,
  `DEFAULT_DRIVE_FOLDER`), so an absent or corrupt file yields today's behaviour.
- Loading trims blanks, de-duplicates and sorts; `drive_folder` is stripped of slashes.
- Saving is explicit ("Save settings"); the status line says changes apply to the next
  scan. Nothing is written implicitly, so a mistyped rule cannot surprise a running job.
- Settings are **global**, not profile fields: scan rules and the Drive default are
  properties of the machine, not of a per-run preset.

## Config tab behaviour

### Google Drive

- Lists remotes from `rclone config dump` as `name (type)`.
- **Add / reconfigure…** launches `rclone config` in a terminal, because Google's OAuth
  consent needs a browser; the app never handles tokens. Terminal selection:
  `$TERMINAL`, else `foot`, `kitty`, `alacritty`, `ghostty`, `konsole`,
  `xfce4-terminal`, `gnome-terminal`, `xdg-terminal-exec`.
- **Test** runs `rclone about <remote>:` and reports used/free/total.
- **Remove** runs `rclone config delete` after a confirmation that states credentials
  only are removed — Drive files are untouched.
- **Default folder** is applied when a remote is chosen from the drive picker
  (`gdrive:FileOrganizer` instead of bare `gdrive:`).

### Scan rules

Two plain-text lists (folders, files), one entry per line, honoured by `walk_files()` via
new `skip_dirs`/`skip_files` parameters (module defaults remain the fallback for callers
and tests). "Reset to defaults" restores the shipped lists and saves.

## Out of scope

- Editing `rclone.conf` or tokens inside the app; OAuth implemented in Python.
- Per-profile scan rules or Drive folder.
- Drag-and-drop between tabs.

## Error handling

- rclone missing → the Drive section says so and `Add / reconfigure…` explains the install.
- No terminal found → dialog with the exact `rclone config` command to run manually.
- Unreadable/invalid settings JSON → defaults, no crash.
- A failed save reports the OS error in the status line and the log. The in-memory rules
  still update so the next scan matches what the user sees, but the failure is explicit —
  a restart would fall back to the last successfully written file.

## Tests

- `SettingsTests`: defaults when absent, round trip, corrupt-file fallback.
- `RcloneConfigHelperTests`: dump parsing/degradation, delete, terminal argv (`TERMINAL`,
  known-terminal fallback, none without rclone).
- `WalkFilesTests`: custom `skip_dirs`/`skip_files` overrides.
- `GoogleDriveGuiTests`: three tabs with the expected titles, settings widgets parented
  into Options/Config, remotes listed in Config, saved scan rules actually used by the
  next scan, and the Drive picker applying the default folder.
