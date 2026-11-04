# File Organizer

A single-file Tkinter desktop app that **copies or moves files of any type** from a
source drive or folder into a destination, sorted into subfolders by date or file type.

![icon](packaging/file-organizer.svg)

## Features

- **All file types** — documents, archives, audio, code, extension-less files, and of
  course photos and videos. One scan lists everything, then per-type checkboxes let you
  include or exclude whole categories before running.
- **9 sort modes** — flat, by year, year/month, year/month/day, year/date, date, file
  type, year → type, type → year. An existing destination layout is detected
  automatically and the matching mode is pre-selected.
- **Real capture dates where available** — EXIF `DateTimeOriginal` for photos (needs
  Pillow), the ISO-BMFF `mvhd` atom for MP4/MOV-family video, filesystem mtime for
  everything else.
- **Duplicate handling** — rename `_2, _3…`, overwrite, skip, or *erase source* when an
  identical copy already exists at the destination (contents compared first).
- **Verification** — off, quick (size + mtime), or SHA-256 bit-exact. Transfer + verify
  removes a source file only after its copy passes; verify-only never deletes anything.
- **USB drives** — attached and unmounted partitions are listed in the drive pickers and
  mounted on selection via `udisksctl`; ⏏ unmounts with an "in use" diagnosis; optionally
  reformats a source drive after a clean run.
- **Remembers where things were** — the source/destination pair is restored on the next
  launch; removable paths are matched by drive label plus relative path, so re-plugged
  drives resolve again.
- **Profiles** — full settings snapshots, saved to `~/.file_organizer/profiles.json`.
- **Theming** — dark/light toggle plus live Omarchy theme following over inotify.

## Requirements

- Python 3.9+ with Tk bindings, standard library only.
  - Arch: `sudo pacman -S tk` · Debian/Ubuntu: `sudo apt install python3-tk`
- Optional: Pillow (`python-pillow` / `pip install Pillow`) for photo EXIF dates.

## Run

```bash
python3 File_Organizer.py
```

## Install as a desktop app (Omarchy / Walker)

```bash
packaging/install.sh              # launcher + icon into ~/.local/share
packaging/install.sh --uninstall  # remove them
```

The generated `.desktop` entry runs the script from this checkout, so keep the repo
where it is (or re-run the installer after moving it).

## Tests

```bash
python3 -m unittest tests.test_organizer -v
```

The suite is headless — it creates no Tk widgets — and covers drive parsing, mount/
unmount diagnosis, duplicate detection, colour math, the theme watcher, persistence,
the all-file scanner, legacy profile migration, packaging assets, and the installer.

## What the scanner skips

`walk_files()` yields every ordinary file but refuses to drag along noise:

- hidden files and directories (any name starting with `.`), so `.git`, `.venv`,
  `.cache`, `.DS_Store`, `.thumbnails` are never copied;
- cross-platform system junk directories: `System Volume Information`, `$RECYCLE.BIN`,
  `__MACOSX`, `.Spotlight-V100`, `.Trashes`, `lost+found`;
- rebuildable dependency/cache trees: `node_modules`, `venv`, `__pycache__`,
  `site-packages`;
- OS metadata files: `Thumbs.db`, `desktop.ini`, `ehthumbs.db`.

## Migrating from Photo Organizer v3

The rebrand moved the profile store from `~/.photo_organizer` to `~/.file_organizer`.
On first launch the old `profiles.json` is **copied over automatically** — including the
remembered source/destination locations — as long as no new store exists yet. The legacy
file is left in place.

If the new app already created a store and you want the old profiles back:

```bash
cp ~/.photo_organizer/profiles.json ~/.file_organizer/profiles.json
```

## Project layout

```
File_Organizer.py                 the whole application (stdlib + optional Pillow)
tests/test_organizer.py           headless unittest suite
packaging/file-organizer.desktop  desktop-entry template (@PROJECT_DIR@ placeholder)
packaging/file-organizer.svg      Omarchy-palette app icon
packaging/install.sh              install/uninstall the launcher
docs/superpowers/                 design specs and implementation plans
```
