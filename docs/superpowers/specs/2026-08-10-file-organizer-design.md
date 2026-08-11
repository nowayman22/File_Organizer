# File Organizer — Design

**Date:** 2026-08-10
**Status:** Approved by user
**Base:** `Organizer_Linux.py` (formerly *Photo Organizer v3*), copied from `/home/nikits/Projects/Organizer`

## Goal

Turn the Photo Organizer into a **File Organizer**: the same GUI app, but it scans and
organizes **every ordinary file type** (documents, archives, audio, code, extension-less
files, …), not just photos and videos. Rebrand fully to "File Organizer".

## Background (what the base already does)

- Single-file Tkinter app (`Organizer_Linux.py`, ~3,300 lines), stdlib + optional Pillow.
- Copies/moves files from a source drive/folder to a destination, sorted into subfolders
  by date or file type (8 sort modes).
- USB drive detection + mount/unmount (udisksctl), duplicate handling (rename/overwrite/
  skip/erase), verify modes (off/quick/SHA-256), profiles, color-coded logging, live
  throughput stats, dark/light themes with Omarchy follow.
- Currently **hard-filtered to media**: `walk_media()` only yields files whose extension
  is in `MEDIA_EXTENSIONS` (photos + videos).
- Already generic today:
  - `build_target_subfolder()` names type folders from any extension (`ext.upper()` or
    `OTHER` for extension-less files).
  - `get_photo_date()` extracts EXIF/MP4 creation dates for media and **falls back to
    filesystem mtime for everything else**.

## Design decisions

### 1. Copy
- Copy the Organizer working tree into `/home/nikits/Projects/File_Organizer` verbatim,
  excluding `__pycache__/`, `*.pyc`, `.superpowers/`, and the old `.git` history.
- Initialize a fresh git repo in the new location.

### 2. All-file scanning
- Rename `walk_media()` → `walk_files()`.
- Yield **every regular file**, any extension or none.
- Still skip:
  - hidden files and directories (name starts with `.`), including dot-files like
    `.DS_Store`, `.thumbnails`, `.gitignore`; and
  - system junk directories: `System Volume Information`, `$RECYCLE.BIN`, `__MACOSX`,
    `.Spotlight-V100`, `.Trashes`, `lost+found`.
- Add a hidden-*file* skip (today only directories are filtered).
- Remove the now-unused `MEDIA_EXTENSIONS` constant. Keep `IMAGE_EXTENSIONS`,
  `VIDEO_EXTENSIONS`, `MP4_LIKE_EXTENSIONS` — they still power EXIF + MP4-family
  creation-date extraction in `get_photo_date()`.

### 3. Date sorting for all files
- No change needed: `get_photo_date()` already returns EXIF date for images, `mvhd`
  creation time for MP4-family videos, and filesystem mtime for all other files.

### 4. Type folders + structure auto-detection
- `build_target_subfolder()` unchanged (already generic).
- Broaden `KNOWN_TYPE_FOLDERS` to recognize common non-media type folders so
  `detect_folder_structure()` still auto-detects a "by file type" layout. Additions
  include (uppercase, no dot): PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, ODT, RTF, TXT, CSV,
  MD, EPUB, ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ, MP3, FLAC, WAV, AAC, OGG, M4A, WMA, ISO,
  DEB, RPM, APPIMAGE, PY, JS, TS, HTML, CSS, JSON, XML, YAML, etc.

### 5. UI copy
- "No media files found." → "No files found."
- Scan summary and log strings: "media files" → "files" (e.g. `Scan complete: {n} files`).

### 6. Full rebrand to File Organizer
- Module docstring → "File Organizer"; description updated to "copy or move files of any
  type". Run instructions updated.
- `PhotoOrganizerApp` → `FileOrganizerApp`.
- Window title → `File Organizer`; header label `📷 Photo Organizer` → `🗂 File Organizer`;
  session-log header → `File Organizer`.
- `PROFILES_DIR` → `~/.file_organizer` (profile JSON lives there).
- Script renamed `Organizer_Linux.py` → `File_Organizer.py`.
- Tests: `import Organizer_Linux as org` → `import File_Organizer as org`; test module
  docstring updated; persistence-test fixture path `.photo_organizer` → `.file_organizer`.

### 7. Tests & verification
- New tests for `walk_files()`:
  - picks up `.pdf`, `.docx`, `.mp3`, a nested file, and an extension-less file;
  - skips hidden files (`.env`, `.DS_Store`) and hidden/system junk dirs.
- New test: `get_photo_date()` on a non-media file returns filesystem mtime.
- Full suite (`python -m unittest discover`) must pass.
- Smoke-check: `python -c "import File_Organizer"`.

## Out of scope
- Configurable type set (approach B) — YAGNI.
- Modular rewrite (approach C).
- No new UI controls; the per-type checkboxes already adapt to whatever types the scan
  finds.

## Error handling & edge cases
- Extension-less files → type folder `OTHER` (existing behavior).
- Uppercase extensions (`.JPG`) are lowercased during the walk (existing behavior).
- Permission errors / unreadable dirs are handled by the existing resilient walk.
