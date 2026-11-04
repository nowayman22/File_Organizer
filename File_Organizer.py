#!/usr/bin/env python3
"""
File Organizer
==============
A modern GUI application to copy or move files of any type from a source
drive or folder to a destination, sorted into folders by date or file type.

v3 additions
------------
  * USB mount/unmount: unmounted USB partitions appear in the drive
    pickers and mount on selection (udisksctl); the ⏏ button safely
    unmounts the selected drive.
  * Live Omarchy theme follow via inotify — restyles the instant the
    theme changes, no polling.
  * 'Erase source' duplicate mode: when an identical copy already exists
    at the destination, the source file is deleted (contents are checked
    first; same-name-different-content files are renamed and kept).
  * Optional format of the source drive after a clean transfer (or
    transfer + verify) run — udisks2 D-Bus Format, asks again before
    erasing, never saved into profiles.
  * Verify-only verbose mode logs each file as present/absent.
  * Detailed, color-coded logging with severity levels (INFO / DEBUG /
    WARN / ERROR / SUCCESS) and per-file detail.
        - Verbose toggle: log every file action with size + timing.
        - Save-to-file toggle: write the session log to a .log file in
          the destination folder.
        - Live throughput stats during transfer / verify (files/s, MB/s).
  * Condensed UI layout: source + destination on one row, options laid
    out in a tight two-column grid, fits comfortably on a 1100x780 window.
  * Profiles: save and load full sets of settings (operation, sort mode,
    duplicate handling, verify mode, paths, toggles, ...) from a
    dropdown in the header. Defaults shipped, custom profiles persist
    to ~/.file_organizer/profiles.json.

v2 features (kept)
------------------
  * Modern dark / light theme with toggle in header
  * Auto-detects existing folder structure in destination, picks a
    matching sort mode automatically
  * Duplicate handling:  Rename with _2, _3, ...   |   Overwrite   |   Skip
  * Resilient scanning (handles permission errors, hidden system folders)

Verify safety
-------------
  Verify-only mode NEVER deletes anything. It compares source files to
  what's already at the destination and reports the result. After verify
  completes, if anything is missing or mismatched, you are offered the
  chance to *copy* those files (never move) — sources are always kept.

  Move + Verify is also safe: each file is copied first, verified, and
  the source is only removed AFTER its copy passes the check. If verify
  fails, the failed copy at the destination is removed and the source
  stays put.

Requirements
------------
  * Python 3.9+  (standard library only)
  * Tk bindings:   Arch -> sudo pacman -S tk
                   Debian/Ubuntu -> sudo apt install python3-tk
  * Optional:   Pillow  (Arch -> sudo pacman -S python-pillow, or pip install Pillow)
      -> enables EXIF "date taken" extraction from photos for accurate
         date-based sorting. Without it, files fall back to the filesystem
         modified time.

Run
---
    python File_Organizer.py
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

try:
    import tkinter as tk
    from tkinter import (filedialog, font as tkfont, messagebox,
                         scrolledtext, simpledialog, ttk)
except ImportError:
    sys.stderr.write(
        "ERROR: Python's Tk bindings (tkinter) are not available.\n"
        "  Arch Linux:    sudo pacman -S tk\n"
        "  Debian/Ubuntu: sudo apt install python3-tk\n"
        "  Fedora:        sudo dnf install python3-tkinter\n")
    raise SystemExit(1)

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# =========================================================================== #
#  Theme palettes                                                              #
# =========================================================================== #
THEMES = {
    "dark": {
        "bg":          "#1e1e2e",
        "bg_alt":      "#181825",
        "surface":     "#313244",
        "surface_hi":  "#45475a",
        "border":      "#45475a",
        "border_hi":   "#585b70",
        "text":        "#cdd6f4",
        "text_muted":  "#9399b2",
        "accent":      "#89b4fa",
        "accent_hi":   "#b4befe",
        "accent_lo":   "#74a3e8",
        "accent_fg":   "#11111b",
        "success":     "#a6e3a1",
        "warning":     "#f9e2af",
        "error":       "#f38ba8",
        "debug":       "#7f849c",
    },
    "light": {
        "bg":          "#f6f7fb",
        "bg_alt":      "#ffffff",
        "surface":     "#ffffff",
        "surface_hi":  "#eef0f6",
        "border":      "#d5d9e2",
        "border_hi":   "#b8bec9",
        "text":        "#1f2937",
        "text_muted":  "#6b7280",
        "accent":      "#3b82f6",
        "accent_hi":   "#2563eb",
        "accent_lo":   "#1d4ed8",
        "accent_fg":   "#ffffff",
        "success":     "#059669",
        "warning":     "#b45309",
        "error":       "#dc2626",
        "debug":       "#9ca3af",
    },
}

# =========================================================================== #
#  Omarchy theme integration                                                   #
# =========================================================================== #
# Omarchy (https://omarchy.org) symlinks the active theme to
# ~/.config/omarchy/current/theme; every theme ships a flat colors.toml.
OMARCHY_COLORS_PATH = Path.home() / ".config" / "omarchy" / "current" / "theme" / "colors.toml"

_TOML_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"([^"]*)"\s*$')
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _parse_simple_toml(text: str) -> dict:
    """Parse flat `key = "value"` lines (Omarchy colors.toml); skip the rest."""
    out: dict = {}
    for line in text.splitlines():
        m = _TOML_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend hex_a toward hex_b by t (0.0 = pure a, 1.0 = pure b)."""
    if not (_HEX_RE.match(hex_a) and _HEX_RE.match(hex_b)):
        raise ValueError(f"invalid hex color: {hex_a!r} / {hex_b!r}")
    t = max(0.0, min(1.0, t))
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(ca + (cb - ca) * t):02x}" for ca, cb in zip(a, b))


def _lighten(color: str, t: float) -> str:
    return _mix(color, "#ffffff", t)


def _darken(color: str, t: float) -> str:
    return _mix(color, "#000000", t)


def load_omarchy_palette(path: Path = OMARCHY_COLORS_PATH) -> Optional[dict]:
    """Build a THEMES-style dark palette from the active Omarchy theme.

    Returns None when the file is missing/unreadable or lacks usable
    background/foreground colors — callers keep the built-in palette then.
    """
    try:
        raw = _parse_simple_toml(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    colors = {k: v for k, v in raw.items() if _HEX_RE.match(v)}
    bg = colors.get("background")
    fg = colors.get("foreground")
    if not bg or not fg:
        return None
    accent = colors.get("accent") or colors.get("color4") or fg
    border = colors.get("color0") or _lighten(bg, 0.16)
    return {
        "bg":          bg,
        "bg_alt":      _darken(bg, 0.20),
        "surface":     _lighten(bg, 0.08),
        "surface_hi":  _lighten(bg, 0.16),
        "border":      border,
        "border_hi":   _lighten(border, 0.15),
        "text":        fg,
        "text_muted":  _mix(fg, bg, 0.45),
        "accent":      accent,
        "accent_hi":   _lighten(accent, 0.15),
        "accent_lo":   _darken(accent, 0.15),
        "accent_fg":   bg,
        "success":     colors.get("color2") or accent,
        "warning":     colors.get("color3") or accent,
        "error":       colors.get("color1") or accent,
        "debug":       _mix(fg, bg, 0.55),
    }


def omarchy_theme_fingerprint(path: Path = OMARCHY_COLORS_PATH) -> Optional[tuple]:
    """(resolved path, mtime) of the active theme's colors.toml, or None.

    The resolved path changes when the Omarchy `current/theme` symlink is
    repointed, the mtime when the file itself is rewritten.
    """
    try:
        real = path.resolve()
        return (str(real), real.stat().st_mtime)
    except OSError:
        return None


# `omarchy theme set` repoints the current/theme symlink inside this dir,
# so one inotify watch here observes every theme switch.
OMARCHY_CURRENT_DIR = OMARCHY_COLORS_PATH.parent.parent

# inotify event masks (linux/inotify.h)
_IN_ATTRIB      = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM  = 0x00000040
_IN_MOVED_TO    = 0x00000080
_IN_CREATE      = 0x00000100
_IN_DELETE      = 0x00000200
_IN_THEME_MASK  = (_IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_FROM
                   | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE)


class Inotify:
    """Minimal ctypes inotify binding (Linux). Raises OSError if unusable."""

    def __init__(self):
        self._libc = ctypes.CDLL(None, use_errno=True)
        fd = self._libc.inotify_init1(os.O_CLOEXEC)  # IN_CLOEXEC == O_CLOEXEC
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd

    def add_watch(self, path: str, mask: int = _IN_THEME_MASK) -> None:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            raise OSError(ctypes.get_errno(),
                          f"inotify_add_watch failed for {path}")

    def wait(self) -> None:
        """Block until any watched path changes (event details don't matter:
        callers re-read the state they care about)."""
        os.read(self.fd, 4096)

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _armed_theme_watcher() -> Inotify:
    """Inotify watching the Omarchy `current` dir and the active theme dir."""
    ino = Inotify()
    try:
        ino.add_watch(str(OMARCHY_CURRENT_DIR))
        try:
            # Also catch in-place edits to the active theme's colors.toml.
            ino.add_watch(str(OMARCHY_COLORS_PATH.resolve().parent))
        except OSError:
            pass
    except OSError:
        ino.close()
        raise
    return ino


def start_omarchy_watcher(notify) -> bool:
    """Call `notify()` from a daemon thread whenever the Omarchy theme changes.

    Returns False when inotify can't watch the Omarchy dirs — callers should
    fall back to polling then. `notify` must be thread-safe (e.g. queue.put).
    """
    try:
        ino = _armed_theme_watcher()
    except OSError:
        return False

    def loop(ino: Optional[Inotify]) -> None:
        while True:
            try:
                if ino is None:
                    ino = _armed_theme_watcher()
                    notify()          # catch up on changes made while blind
                ino.wait()
                time.sleep(0.2)       # let the event burst settle
                ino.close()
                ino = None
                ino = _armed_theme_watcher()  # re-arm before notifying so no
                notify()                      # change slips through unwatched
            except OSError:
                if ino is not None:
                    ino.close()
                    ino = None
                time.sleep(5)

    threading.Thread(target=loop, args=(ino,), daemon=True,
                     name="omarchy-theme-watch").start()
    return True


# Defaults are Windows fonts; resolve_fonts() overrides them at startup with
# families that actually exist on this machine (see below). Kept as a fallback
# in case resolution runs before a Tk root exists.
UI_FONT         = ("Segoe UI", 10)
UI_FONT_BOLD    = ("Segoe UI", 10, "bold")
UI_FONT_SEMI    = ("Segoe UI Semibold", 10)
UI_FONT_TITLE   = ("Segoe UI", 16, "bold")
UI_FONT_HEADING = ("Segoe UI", 10, "bold")
UI_FONT_SMALL   = ("Segoe UI", 9)
UI_FONT_MONO    = ("Consolas", 9)

# Preference lists: Windows names first (no-ops elsewhere), then the common
# Linux/Arch sans and monospace families, newest/nicest first.
_SANS_CANDIDATES = [
    "Segoe UI", "Cantarell", "Inter", "Adwaita Sans", "Noto Sans",
    "DejaVu Sans", "Liberation Sans", "Ubuntu", "Roboto",
]
_MONO_CANDIDATES = [
    "Consolas", "JetBrainsMono Nerd Font", "JetBrains Mono", "CaskaydiaMono Nerd Font",
    "Cascadia Code", "Hack", "Fira Code", "Source Code Pro", "Adwaita Mono",
    "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono",
]


def _pick_family(candidates: list[str], named_fallback: str) -> str:
    """Return the first installed family from `candidates`.

    Falls back to whatever Tk uses for the given named font (e.g.
    'TkDefaultFont'), so we always hand Tk a real, resolvable family.
    """
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return candidates[0]
    for name in candidates:
        if name in available:
            return name
    try:
        return tkfont.nametofont(named_fallback).actual("family")
    except tk.TclError:
        return candidates[-1]


def resolve_fonts() -> None:
    """Rebind the UI_FONT_* globals to families present on this system.

    Requires an existing Tk root (call after tk.Tk()). On Arch this swaps the
    Windows defaults for e.g. Noto Sans + JetBrainsMono Nerd Font; sizes are
    kept identical so the tuned window layout is unchanged.
    """
    global UI_FONT, UI_FONT_BOLD, UI_FONT_SEMI, UI_FONT_TITLE
    global UI_FONT_HEADING, UI_FONT_SMALL, UI_FONT_MONO

    sans = _pick_family(_SANS_CANDIDATES, "TkDefaultFont")
    mono = _pick_family(_MONO_CANDIDATES, "TkFixedFont")

    UI_FONT         = (sans, 10)
    UI_FONT_BOLD    = (sans, 10, "bold")
    UI_FONT_SEMI    = (sans, 10, "bold")
    UI_FONT_TITLE   = (sans, 16, "bold")
    UI_FONT_HEADING = (sans, 10, "bold")
    UI_FONT_SMALL   = (sans, 9)
    UI_FONT_MONO    = (mono, 9)


# =========================================================================== #
# Extensions (used for embedded-date extraction)                               #
# =========================================================================== #
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif",
    ".raw", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".dng", ".orf", ".rw2", ".pef", ".srw", ".raf", ".3fr",
    ".erf", ".kdc", ".mrw", ".rwl", ".x3f",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv",
    ".3gp", ".mpg", ".mpeg", ".webm", ".m2ts", ".mts", ".vob",
}

# Video formats whose creation date lives in the ISO-BMFF 'mvhd' atom.
# Everything here can be parsed with pure stdlib (no external dependencies).
MP4_LIKE_EXTENSIONS = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".m4a"}

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

SORT_MODES = [
    ("none",             "No subfolders (flat)"),
    ("year",             "By Year                   →  2024/"),
    ("year_month",       "By Year / Month           →  2024/01-January/"),
    ("year_month_day",   "By Year / Month / Day     →  2024/01/15/"),
    ("year_then_date",   "Year / YYYY-MM-DD         →  2026/2026-02-14/"),
    ("date_only",        "YYYY-MM-DD (flat)         →  2026-02-14/"),
    ("file_type",        "By File Type              →  JPG/, PNG/, MP4/"),
    ("year_then_type",   "Year → File Type          →  2024/JPG/"),
    ("type_then_year",   "File Type → Year          →  JPG/2024/"),
]
SORT_LABEL_BY_KEY = {k: lbl for k, lbl in SORT_MODES}
SORT_KEY_BY_LABEL = {lbl: k for k, lbl in SORT_MODES}

DUP_MODES = [
    ("rename",    "Rename _2, _3…"),
    ("overwrite", "Overwrite"),
    ("skip",      "Skip"),
    ("erase",     "Erase source"),
]

VERIFY_MODES = [
    ("off",   "Off"),
    ("quick", "Quick (size + mtime)"),
    ("hash",  "SHA-256 (bit-exact)"),
]

RUN_MODES = [
    ("transfer",          "Transfer only"),
    ("verify",            "Verify only"),
    ("transfer_verify",   "Transfer + verify"),
]

# Type folder names we recognize when auto-detecting
KNOWN_TYPE_FOLDERS = {
    "JPG", "JPEG", "PNG", "HEIC", "HEIF", "RAW", "CR2", "CR3", "NEF", "NRW",
    "ARW", "SRF", "SR2", "DNG", "ORF", "RW2", "PEF", "SRW", "RAF", "3FR",
    "ERF", "KDC", "MRW", "RWL", "X3F", "TIFF", "TIF", "GIF", "WEBP", "BMP",
    "JFIF", "MP4", "M4V", "MOV", "AVI", "MKV", "WMV", "FLV", "3GP", "MPG",
    "MPEG", "WEBM", "M2TS", "MTS", "VOB",
    "PDF", "DOC", "DOCX", "XLS", "XLSX", "PPT", "PPTX", "ODT", "ODS",
    "RTF", "TXT", "CSV", "MD", "EPUB",
    "ZIP", "RAR", "7Z", "TAR", "GZ", "BZ2", "XZ", "ISO",
    "MP3", "FLAC", "WAV", "AAC", "OGG", "M4A", "WMA",
    "PY", "JS", "TS", "HTML", "CSS", "JSON", "XML", "YAML", "SH", "BAT",
    "DB", "SQL", "EXE", "MSI", "DEB", "RPM", "APPIMAGE",
    "PSD", "AI", "SVG", "TTF", "OTF",
    "LOG", "INI", "CFG", "CONF", "TORRENT",
}


# =========================================================================== #
#  Profiles                                                                    #
# =========================================================================== #
PROFILES_DIR  = Path.home() / ".file_organizer"
PROFILES_FILE = PROFILES_DIR / "profiles.json"

# Store used by the pre-rebrand app ("Photo Organizer v3"). Its profiles.json
# also holds the remembered source/destination locations, so it is migrated
# once into the new store rather than silently abandoned after the rename.
LEGACY_PROFILES_DIR_NAME = ".photo_organizer"

# Keys saved/restored when a profile is applied.
PROFILE_FIELDS = (
    "source_path", "dest_path",
    "operation", "run_mode", "sort_mode", "dup_mode", "verify_mode",
    "include_subdirs", "sync_writes", "verbose_log", "log_to_file", "theme",
)

# Built-in profiles. Cannot be deleted, but can be overridden by user
# saving a profile with the same name.
DEFAULT_PROFILES: dict[str, dict] = {
    "Default — Copy by Year/Month": {
        "operation": "copy",
        "run_mode": "transfer",
        "sort_mode": "year_month",
        "dup_mode": "rename",
        "verify_mode": "quick",
        "include_subdirs": True,
        "sync_writes": True,
        "verbose_log": False,
        "log_to_file": False,
    },
    "Safe Move (verify before delete)": {
        "operation": "move",
        "run_mode": "transfer_verify",
        "sort_mode": "year_month",
        "dup_mode": "rename",
        "verify_mode": "hash",
        "include_subdirs": True,
        "sync_writes": True,
        "verbose_log": True,
        "log_to_file": True,
    },
    "Verify only (read-only check)": {
        "operation": "copy",
        "run_mode": "verify",
        "sort_mode": "year_month",
        "dup_mode": "rename",
        "verify_mode": "hash",
        "include_subdirs": True,
        "sync_writes": True,
        "verbose_log": True,
        "log_to_file": False,
    },
    "Sort by File Type": {
        "operation": "copy",
        "run_mode": "transfer",
        "sort_mode": "file_type",
        "dup_mode": "rename",
        "verify_mode": "quick",
        "include_subdirs": True,
        "sync_writes": True,
        "verbose_log": False,
        "log_to_file": False,
    },
    "Flat dump (no subfolders)": {
        "operation": "copy",
        "run_mode": "transfer",
        "sort_mode": "none",
        "dup_mode": "rename",
        "verify_mode": "off",
        "include_subdirs": True,
        "sync_writes": True,
        "verbose_log": False,
        "log_to_file": False,
    },
}


def legacy_profiles_file() -> Path:
    """Path of the pre-rebrand profiles.json sitting beside PROFILES_DIR."""
    return PROFILES_DIR.parent / LEGACY_PROFILES_DIR_NAME / "profiles.json"


def migrate_legacy_profiles() -> bool:
    """Copy the pre-rebrand profiles.json into the new store, once.

    Preserves custom profiles and the remembered locations across the
    "Photo Organizer" -> "File Organizer" rename. Never overwrites an
    existing new-store file, never deletes the legacy file, and refuses
    to copy a file that is not a JSON object. Returns True on migration.
    """
    source = legacy_profiles_file()
    if PROFILES_FILE.exists() or not source.exists():
        return False
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, PROFILES_FILE)
        return True
    except OSError:
        return False


def load_profiles_from_disk() -> tuple[dict[str, dict], Optional[str], dict]:
    """Return (profiles, last_used_name, last_locations).

    Migrates the pre-rebrand store on first use, then falls back to
    defaults / empty on any error.
    """
    migrate_legacy_profiles()
    profiles: dict[str, dict] = {name: dict(data)
                                 for name, data in DEFAULT_PROFILES.items()}
    last_used: Optional[str] = None
    last_locations: dict = {}
    if not PROFILES_FILE.exists():
        return profiles, last_used, last_locations
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        custom = raw.get("profiles") or {}
        if isinstance(custom, dict):
            for name, data in custom.items():
                if isinstance(name, str) and isinstance(data, dict):
                    profiles[name] = data
        lu = raw.get("last_used")
        if isinstance(lu, str):
            last_used = lu
        ll = raw.get("last_locations")
        if isinstance(ll, dict):
            last_locations = {k: v for k, v in ll.items()
                              if k in ("source", "dest")
                              and isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return profiles, last_used, last_locations


def save_profiles_to_disk(profiles: dict[str, dict], last_used: Optional[str],
                          last_locations: dict) -> Optional[str]:
    """Persist non-default (or overridden) profiles. Returns error string or None."""
    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        # Only persist custom profiles + names that override defaults with changes.
        to_save: dict[str, dict] = {}
        for name, data in profiles.items():
            if name in DEFAULT_PROFILES and data == DEFAULT_PROFILES[name]:
                continue
            to_save[name] = data
        payload = {"profiles": to_save, "last_used": last_used,
                   "last_locations": last_locations or {}}
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return None
    except OSError as e:
        return str(e)


# =========================================================================== #
#  Removable-drive detection                                                   #
# =========================================================================== #
class UsbDrive(NamedTuple):
    label: str
    mountpoint: str  # "" while the partition is not mounted
    size: int        # bytes
    free: int        # bytes (0 while not mounted)
    device: str      # kernel name, e.g. "sda1" ("" for fallback scan)
    fstype: str = "" # e.g. "exfat" ("" when unknown)

    @property
    def mounted(self) -> bool:
        return bool(self.mountpoint)


# Mountpoints that are never a photo source/destination, even on a
# removable-flagged device (some SATA/eSATA disks report rm=1).
_SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/var", "[SWAP]"}

# Filesystems udisksctl can't (or shouldn't) mount as a photo drive.
_NONMOUNTABLE_FS = {"swap", "crypto_LUKS", "LVM2_member", "linux_raid_member"}


def _coerce_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_bool(value) -> bool:
    # lsblk emits real booleans in current util-linux, "0"/"1" strings in
    # older releases.
    if isinstance(value, bool):
        return value
    return str(value).strip() in ("1", "true")


def parse_lsblk_drives(data: dict) -> list[UsbDrive]:
    """Extract USB/removable partitions from parsed `lsblk -J -b`.

    Mounted partitions carry their mountpoint; unmounted ones with a
    mountable filesystem are included with mountpoint "" so the UI can
    offer to mount them. The usb/removable property usually sits on the
    parent disk while the filesystem sits on the child partition, so it
    is inherited downward.
    """
    drives: list[UsbDrive] = []

    def walk(node, inherited: bool) -> None:
        if not isinstance(node, dict):
            return
        is_usb = (inherited or node.get("tran") == "usb"
                  or _coerce_bool(node.get("rm")))
        mp = node.get("mountpoint")
        fstype = node.get("fstype")
        if (is_usb and isinstance(mp, str) and mp
                and mp not in _SYSTEM_MOUNTS and not mp.startswith("/snap")):
            name = node.get("name") or ""
            label = node.get("label") or Path(mp).name or name
            drives.append(UsbDrive(
                label=label,
                mountpoint=mp,
                size=_coerce_int(node.get("size")),
                free=_coerce_int(node.get("fsavail")),
                device=name,
                fstype=fstype if isinstance(fstype, str) else "",
            ))
        elif (is_usb and not mp and isinstance(fstype, str) and fstype
                and fstype not in _NONMOUNTABLE_FS):
            name = node.get("name") or ""
            drives.append(UsbDrive(
                label=node.get("label") or name,
                mountpoint="",
                size=_coerce_int(node.get("size")),
                free=0,
                device=name,
                fstype=fstype,
            ))
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                walk(child, is_usb)

    devices = data.get("blockdevices") if isinstance(data, dict) else None
    if isinstance(devices, list):
        for dev in devices:
            walk(dev, False)
    return drives


def _media_roots() -> list[Path]:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return [Path("/run/media") / user, Path("/media") / user]


def _scan_media_dirs() -> list[UsbDrive]:
    """Fallback when lsblk is unavailable: udisks mount dirs + statvfs."""
    drives: list[UsbDrive] = []
    seen: set[str] = set()
    for root in _media_roots():
        try:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for p in entries:
            mp = str(p)
            if mp in seen:
                continue
            try:
                if not p.is_dir():
                    continue
                st = os.statvfs(mp)
            except OSError:
                continue
            seen.add(mp)
            drives.append(UsbDrive(
                label=p.name,
                mountpoint=mp,
                size=st.f_frsize * st.f_blocks,
                free=st.f_frsize * st.f_bavail,
                device="",
            ))
    return drives


def list_usb_drives() -> list[UsbDrive]:
    """All USB/removable drives, mounted or not. Never raises."""
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,LABEL,MOUNTPOINT,SIZE,FSAVAIL,RM,TRAN,TYPE,FSTYPE"],
            capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            return parse_lsblk_drives(json.loads(proc.stdout))
    except (OSError, subprocess.SubprocessError,
            json.JSONDecodeError, ValueError):
        pass
    return _scan_media_dirs()


def _udisksctl(verb: str, device: str) -> tuple[bool, str]:
    """Run `udisksctl <verb> -b /dev/<device>`; returns (ok, message).

    --no-user-interaction keeps polkit from popping an auth dialog (and
    from blocking forever without one); removable drives don't need it.
    """
    try:
        proc = subprocess.run(
            ["udisksctl", verb, "-b", f"/dev/{device}",
             "--no-user-interaction"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    msg = " ".join(s.strip() for s in (proc.stdout, proc.stderr) if s.strip())
    return proc.returncode == 0, msg


# "Mounted /dev/sdb1 at /run/media/user/LABEL" — udisks2 < 2.9 adds a
# trailing period after the path.
_MOUNTED_AT_RE = re.compile(r" at (.+?)\.?\s*$")


def mount_drive(device: str) -> tuple[bool, str]:
    """Mount /dev/<device> via udisks: (True, mountpoint) or (False, error)."""
    ok, msg = _udisksctl("mount", device)
    if ok:
        m = _MOUNTED_AT_RE.search(msg)
        if m:
            return True, m.group(1)
    return ok, msg


def unmount_drive(device: str) -> tuple[bool, str]:
    """Unmount /dev/<device> via udisks: (ok, human-readable message)."""
    return _udisksctl("unmount", device)


def processes_using_mount(mountpoint: str) -> list[str]:
    """Best-effort list of processes holding something open under a mount.

    Scans /proc for open fds and working directories below `mountpoint`.
    Only same-user processes are readable — which is exactly the set that
    can block a udisks unmount of a user-mounted drive (file managers,
    shells cd'd into it, this app itself).
    """
    root = mountpoint.rstrip("/")
    prefix = root + "/"
    hits: list[str] = []
    try:
        piddirs = list(Path("/proc").iterdir())
    except OSError:
        return hits
    for piddir in piddirs:
        if not piddir.name.isdigit():
            continue
        found = None
        try:
            cwd = os.readlink(piddir / "cwd")
            if cwd == root or cwd.startswith(prefix):
                found = f"working directory {cwd}"
            else:
                for fd in (piddir / "fd").iterdir():
                    try:
                        tgt = os.readlink(fd)
                    except OSError:
                        continue
                    if tgt == root or tgt.startswith(prefix):
                        found = f"open file {tgt}"
                        break
        except OSError:
            continue
        if found:
            try:
                name = (piddir / "comm").read_text().strip()
            except OSError:
                name = "?"
            me = " (this app)" if int(piddir.name) == os.getpid() else ""
            hits.append(f"{name}[{piddir.name}]{me}: {found}")
    return hits


def unmount_drive_with_diagnosis(device: str,
                                 mountpoint: str) -> tuple[bool, str]:
    """unmount_drive, but retries once on 'busy' and names the culprits.

    A busy target right after a transfer is often transient (handles
    being released, writeback finishing); if it persists, the message
    lists which processes hold the drive so the user can close them.
    """
    ok, msg = unmount_drive(device)
    if ok or "busy" not in msg.lower():
        return ok, msg
    time.sleep(1.0)
    ok, msg = unmount_drive(device)
    if ok or "busy" not in msg.lower():
        return ok, msg
    holders = processes_using_mount(mountpoint)
    if holders:
        msg += "  —  in use by: " + ";  ".join(holders[:6])
    return False, msg


# Characters udisks/mkfs accept in a filesystem label (also keeps the
# gdbus GVariant literal below injection-safe).
_LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9 _.-]")


def format_drive(device: str, fstype: str, label: str = "") -> tuple[bool, str]:
    """Re-create the filesystem on /dev/<device>, erasing everything.

    udisksctl has no format verb, so this calls udisks2's D-Bus method
    Block.Format directly (same polkit rules as mount/unmount: no root
    needed for removable drives) via busctl, falling back to gdbus.
    Returns (ok, message).
    """
    if not device or not fstype:
        return False, "unknown device or filesystem type"
    label = _LABEL_SAFE_RE.sub("", label)[:32]
    obj = f"/org/freedesktop/UDisks2/block_devices/{device}"
    busctl = ["busctl", "call", "--system", "--timeout=300",
              "org.freedesktop.UDisks2", obj,
              "org.freedesktop.UDisks2.Block", "Format", "sa{sv}", fstype]
    busctl += ["1", "label", "s", label] if label else ["0"]
    gdbus = ["gdbus", "call", "--system",
             "--dest", "org.freedesktop.UDisks2",
             "--object-path", obj,
             "--method", "org.freedesktop.UDisks2.Block.Format",
             fstype, "{'label': <'%s'>}" % label if label else "{}"]
    for cmd in (busctl, gdbus):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300)
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        msg = " ".join(s.strip() for s in (proc.stdout, proc.stderr)
                       if s.strip())
        ok = proc.returncode == 0
        return ok, msg or ("formatted" if ok else "format failed")
    return False, "neither busctl nor gdbus is available"


def files_identical(src: Path, dst: Path, verify: str) -> bool:
    """Content check for the 'erase' duplicate mode.

    Bit-exact (SHA-256) when the run verifies by hash, size + mtime
    otherwise. Any I/O error counts as 'different' — never a reason to
    delete a source file.
    """
    mode = "hash" if verify == "hash" else "quick"
    ok, _reason = verify_copy(src, dst, mode)
    return ok


def make_location_record(path_str: str, drives: list[UsbDrive]) -> dict:
    """Describe a chosen path so it can be restored after a remount.

    If the path sits on a known removable drive, remember the drive label
    and the path relative to the mountpoint; the raw path is always kept
    for fixed folders like ~/Pictures.
    """
    record = {"path": path_str}
    try:
        p = Path(path_str).resolve()
    except OSError:
        return record
    best: Optional[UsbDrive] = None
    for d in drives:
        if not d.mounted:
            continue
        try:
            # Resolve mountpoint to handle symlinked mount points
            resolved_mountpoint = Path(d.mountpoint).resolve()
        except OSError:
            # If resolve() fails, skip this drive
            continue
        try:
            p.relative_to(resolved_mountpoint)
        except ValueError:
            continue
        if best is None or len(resolved_mountpoint.as_posix()) > len(best.mountpoint):
            best = d      # deepest matching mountpoint wins
    if best is not None:
        record["label"] = best.label
        try:
            resolved_mountpoint = Path(best.mountpoint).resolve()
            record["rel"] = str(p.relative_to(resolved_mountpoint))
        except OSError:
            # If we can't resolve the mountpoint here, use the original
            record["rel"] = str(p.relative_to(Path(best.mountpoint)))
    return record


def resolve_location(record, drives: list[UsbDrive]
                     ) -> tuple[Optional[str], Optional[str]]:
    """Turn a saved location record back into a usable path.

    Returns (path, None) on success, (None, label) when the remembered
    drive isn't connected, (None, None) when there is nothing to restore.
    """
    if not isinstance(record, dict):
        return None, None
    path = record.get("path")
    if isinstance(path, str) and path and Path(path).is_dir():
        return path, None
    label = record.get("label")
    if not isinstance(label, str) or not label:
        return None, None
    for d in drives:
        if d.label == label and d.mounted:
            rel = record.get("rel")
            if isinstance(rel, str) and rel not in ("", "."):
                candidate = Path(d.mountpoint) / rel
                if candidate.is_dir():
                    return str(candidate), None
            # rel == "." means the mountpoint itself — fall through
            return d.mountpoint, None
    return None, label


# =========================================================================== #
#  Helpers                                                                     #
# =========================================================================== #
# Epoch for MP4/MOV 'mvhd' creation timestamps: 1904-01-01 UTC
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _scan_moov_for_mvhd(f, end: int) -> Optional[datetime]:
    """Inside a moov atom, find the mvhd and return its creation time."""
    while f.tell() < end:
        sub = f.read(8)
        if len(sub) < 8:
            return None
        sub_size = int.from_bytes(sub[:4], "big")
        sub_type = sub[4:8]
        sub_header = 8
        if sub_size == 1:                      # 64-bit extended size
            ext = f.read(8)
            if len(ext) < 8:
                return None
            sub_size = int.from_bytes(ext, "big")
            sub_header = 16
        if sub_size < sub_header:
            return None

        if sub_type == b"mvhd":
            vflags = f.read(4)
            if len(vflags) < 4:
                return None
            version = vflags[0]
            if version == 1:                   # 64-bit time fields
                raw = f.read(8)
                if len(raw) < 8:
                    return None
                ct = int.from_bytes(raw, "big")
            else:                              # 32-bit time fields
                raw = f.read(4)
                if len(raw) < 4:
                    return None
                ct = int.from_bytes(raw, "big")
            if ct == 0:
                return None
            try:
                utc = _MP4_EPOCH + timedelta(seconds=ct)
                if utc.year < 1970 or utc.year > 2200:
                    return None
                return utc.astimezone().replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            f.seek(sub_size - sub_header, 1)
    return None


def get_mp4_creation_time(path: Path) -> Optional[datetime]:
    """Read creation time from an MP4/MOV/M4V/3GP file's mvhd atom."""
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return None
                size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8]
                header_size = 8
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        return None
                    size = int.from_bytes(ext, "big")
                    header_size = 16
                elif size == 0:
                    return None
                if size < header_size:
                    return None

                if atom_type == b"moov":
                    return _scan_moov_for_mvhd(f, f.tell() + size - header_size)
                f.seek(size - header_size, 1)
    except (OSError, struct.error, ValueError):
        return None


def get_photo_date(path: Path) -> datetime:
    """Best-effort capture date for any file.

    Photos may have EXIF, MP4-family videos may have an embedded 'mvhd'
    creation time; everything else falls back to filesystem mtime.

    Priority:
      1) EXIF DateTimeOriginal  (images, requires Pillow)
      2) MP4/MOV 'mvhd' atom creation time  (videos, stdlib-only)
      3) Filesystem modified time
    """
    ext = path.suffix.lower()

    if PIL_AVAILABLE and ext in IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as img:
                exif = img._getexif() or {}
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    try:
                        return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
        except Exception:
            pass

    if ext in MP4_LIKE_EXTENSIONS:
        dt = get_mp4_creation_time(path)
        if dt is not None:
            return dt

    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.now()


def unique_destination(dest: Path) -> Path:
    """Append _2, _3, ... to avoid name collisions. Starts at _2."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def default_source_dir() -> str:
    """Best first directory for the source picker.

    On Arch, udisks mounts removable drives (SD cards, cameras, USB sticks)
    under /run/media/<user>/ — the most likely home of a photo dump. Fall back
    through the other common mount roots, then the user's home.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    for cand in (Path("/run/media") / user, Path("/media") / user,
                 Path("/run/media"), Path("/media"), Path("/mnt")):
        try:
            if cand.is_dir() and any(cand.iterdir()):
                return str(cand)
        except OSError:
            continue
    return str(Path.home())


def default_dest_dir() -> str:
    """Best first directory for the destination picker: ~/Pictures, else home."""
    pics = Path.home() / "Pictures"
    return str(pics if pics.is_dir() else Path.home())


def human_size(num_bytes: int) -> str:
    f = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def human_rate(bytes_per_sec: float) -> str:
    """Format throughput like '12.4 MB/s'."""
    if bytes_per_sec <= 0:
        return "—"
    return f"{human_size(int(bytes_per_sec))}/s"


def human_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds*1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def flush_to_disk(path: Path, drop_cache: bool = False) -> Optional[str]:
    """fsync a freshly written file so its data is on the device.

    USB sticks otherwise report success while the data still sits in the
    page cache — fatal in move mode, where the source is deleted next.
    With drop_cache, also evict the file from the cache so a following
    SHA-256 verify reads back from the physical device instead of RAM.
    Returns an error string on failure, None on success.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as e:
        return str(e)
    try:
        os.fsync(fd)
        if drop_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError as e:
        return str(e)
    finally:
        os.close(fd)
    return None


def verify_copy(src: Path, dst: Path, mode: str) -> tuple[bool, str]:
    """Return (ok, reason). `reason` is '' on success."""
    if mode == "off":
        return True, ""
    try:
        if not dst.exists():
            return False, "destination missing"
        s_stat = src.stat()
        d_stat = dst.stat()
        if s_stat.st_size != d_stat.st_size:
            return False, f"size mismatch ({s_stat.st_size} vs {d_stat.st_size})"
        if mode == "quick":
            if abs(s_stat.st_mtime - d_stat.st_mtime) > 2:
                return False, "mtime mismatch"
            return True, ""
        if mode == "hash":
            if sha256_of(src) != sha256_of(dst):
                return False, "hash mismatch"
            return True, ""
    except OSError as e:
        return False, f"io error: {e}"
    return True, ""


def build_target_subfolder(file_path: Path, ext: str, sort_mode: str) -> Path:
    """Return a relative Path describing the sub-folder for this file."""
    if sort_mode == "none":
        return Path(".")

    type_folder = ext.lstrip(".").upper() or "OTHER"
    if sort_mode == "file_type":
        return Path(type_folder)

    dt = get_photo_date(file_path)
    year = f"{dt.year:04d}"
    month_named = f"{dt.month:02d}-{MONTH_NAMES[dt.month]}"
    month_num = f"{dt.month:02d}"
    day = f"{dt.day:02d}"

    if sort_mode == "year":            return Path(year)
    if sort_mode == "year_month":      return Path(year) / month_named
    if sort_mode == "year_month_day":  return Path(year) / month_num / day
    if sort_mode == "year_then_date":  return Path(year) / f"{year}-{month_num}-{day}"
    if sort_mode == "date_only":       return Path(f"{year}-{month_num}-{day}")
    if sort_mode == "year_then_type":  return Path(year) / type_folder
    if sort_mode == "type_then_year":  return Path(type_folder) / year
    return Path(".")


def detect_folder_structure(dest: Path) -> Optional[str]:
    """Look at existing folders in `dest` and guess the sort-mode key."""
    if not dest or not dest.exists() or not dest.is_dir():
        return None
    try:
        top = [p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except (PermissionError, OSError):
        return None
    if not top:
        return None

    year_re = re.compile(r"^(19|20)\d{2}$")
    date_re = re.compile(r"^(19|20)\d{2}[-_]\d{1,2}[-_]\d{1,2}$")

    year_dirs = [p for p in top if year_re.match(p.name)]
    date_dirs = [p for p in top if date_re.match(p.name)]
    type_dirs = [p for p in top if p.name.upper() in KNOWN_TYPE_FOLDERS]

    if (len(date_dirs) >= 1
            and len(date_dirs) >= len(year_dirs)
            and len(date_dirs) >= len(type_dirs)):
        return "date_only"

    if len(year_dirs) >= 1 and len(year_dirs) >= len(type_dirs):
        sample = year_dirs[0]
        try:
            subs = [p for p in sample.iterdir() if p.is_dir()]
        except OSError:
            return "year"
        if not subs:
            return "year"

        if any(date_re.match(s.name) for s in subs):
            return "year_then_date"

        named_month = any(re.match(r"^\d{1,2}[-_]", s.name) for s in subs)
        numeric_month = any(re.match(r"^\d{1,2}$", s.name) for s in subs)
        type_sub = any(s.name.upper() in KNOWN_TYPE_FOLDERS for s in subs)

        if named_month:
            return "year_month"
        if numeric_month:
            num_sub = next((s for s in subs if re.match(r"^\d{1,2}$", s.name)), None)
            try:
                if num_sub and any(re.match(r"^\d{1,2}$", p.name)
                                    for p in num_sub.iterdir() if p.is_dir()):
                    return "year_month_day"
            except OSError:
                pass
            return "year_month"
        if type_sub:
            return "year_then_type"
        return "year"

    if len(type_dirs) >= 1:
        sample = type_dirs[0]
        try:
            subs = [p for p in sample.iterdir() if p.is_dir()]
            if any(year_re.match(s.name) for s in subs):
                return "type_then_year"
        except OSError:
            pass
        return "file_type"

    return None


# Noise a general-purpose organizer must never sweep into the destination.
# Dot-prefixed names (.git, .venv, .cache) are already skipped by the walk.
SKIPPED_DIRS = {
    # cross-platform system junk
    "System Volume Information", "$RECYCLE.BIN", "__MACOSX",
    ".Spotlight-V100", ".Trashes", "lost+found",
    # rebuildable dependency/cache trees, often huge and never user content
    "node_modules", "venv", "__pycache__", "site-packages",
}

# OS metadata files that carry no user content.
SKIPPED_FILES = {"Thumbs.db", "desktop.ini", "ehthumbs.db"}


def walk_files(source: Path, recurse: bool, cancel_event: threading.Event):
    """Yield every ordinary file under `source`.

    Skips hidden files/dirs (leading '.'), system junk directories, and
    rebuildable dependency trees. Resilient against permission errors.
    """
    if recurse:
        for root, dirs, files in os.walk(source, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in SKIPPED_DIRS]
            if cancel_event.is_set():
                return
            for name in files:
                if name.startswith(".") or name in SKIPPED_FILES:
                    continue
                yield Path(root) / name
    else:
        try:
            for p in source.iterdir():
                if cancel_event.is_set():
                    return
                if (p.is_file() and not p.name.startswith(".")
                        and p.name not in SKIPPED_FILES):
                    yield p
        except (PermissionError, OSError):
            return


# =========================================================================== #
#  Theme engine                                                                #
# =========================================================================== #
def apply_theme(root: tk.Tk, colors: dict) -> None:
    """Apply a color palette to every style Tkinter will respect."""
    root.configure(bg=colors["bg"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(
        ".",
        background=colors["bg"],
        foreground=colors["text"],
        fieldbackground=colors["bg_alt"],
        bordercolor=colors["border"],
        darkcolor=colors["bg"],
        lightcolor=colors["bg"],
        troughcolor=colors["bg_alt"],
        selectbackground=colors["accent"],
        selectforeground=colors["accent_fg"],
        focuscolor=colors["accent"],
        font=UI_FONT,
    )

    style.configure("TFrame",         background=colors["bg"])
    style.configure("Header.TFrame",  background=colors["bg"])
    style.configure("Card.TFrame",    background=colors["bg"])

    style.configure("TLabel",
                    background=colors["bg"], foreground=colors["text"], font=UI_FONT)
    style.configure("Muted.TLabel",
                    background=colors["bg"], foreground=colors["text_muted"], font=UI_FONT_SMALL)
    style.configure("Title.TLabel",
                    background=colors["bg"], foreground=colors["text"], font=UI_FONT_TITLE)
    style.configure("Heading.TLabel",
                    background=colors["bg"], foreground=colors["accent"], font=UI_FONT_HEADING)
    style.configure("Hint.TLabel",
                    background=colors["bg"], foreground=colors["success"], font=UI_FONT_SMALL)

    style.configure("Card.TLabelframe",
                    background=colors["bg"],
                    bordercolor=colors["border"],
                    lightcolor=colors["border"],
                    darkcolor=colors["border"],
                    relief="solid",
                    borderwidth=1,
                    padding=10)
    style.configure("Card.TLabelframe.Label",
                    background=colors["bg"],
                    foreground=colors["accent"],
                    font=UI_FONT_HEADING,
                    padding=(4, 0))

    style.configure("TEntry",
                    fieldbackground=colors["bg_alt"],
                    foreground=colors["text"],
                    bordercolor=colors["border"],
                    lightcolor=colors["border"],
                    darkcolor=colors["border"],
                    insertcolor=colors["text"],
                    padding=6)
    style.map("TEntry",
              bordercolor=[("focus", colors["accent"])],
              lightcolor=[("focus", colors["accent"])],
              darkcolor=[("focus", colors["accent"])])

    style.configure("TButton",
                    background=colors["surface"],
                    foreground=colors["text"],
                    bordercolor=colors["border"],
                    lightcolor=colors["surface"],
                    darkcolor=colors["surface"],
                    padding=(12, 6),
                    relief="flat",
                    font=UI_FONT)
    style.map("TButton",
              background=[("active", colors["surface_hi"]),
                          ("pressed", colors["surface_hi"]),
                          ("disabled", colors["bg_alt"])],
              foreground=[("disabled", colors["text_muted"])],
              bordercolor=[("active", colors["border_hi"]),
                           ("focus", colors["accent"])])

    style.configure("Accent.TButton",
                    background=colors["accent"],
                    foreground=colors["accent_fg"],
                    bordercolor=colors["accent"],
                    lightcolor=colors["accent"],
                    darkcolor=colors["accent"],
                    padding=(16, 8),
                    relief="flat",
                    font=UI_FONT_BOLD)
    style.map("Accent.TButton",
              background=[("active", colors["accent_hi"]),
                          ("pressed", colors["accent_lo"]),
                          ("disabled", colors["surface"])],
              foreground=[("disabled", colors["text_muted"])],
              bordercolor=[("active", colors["accent_hi"])])

    style.configure("Ghost.TButton",
                    background=colors["bg"],
                    foreground=colors["text_muted"],
                    bordercolor=colors["bg"],
                    lightcolor=colors["bg"],
                    darkcolor=colors["bg"],
                    padding=(8, 5),
                    relief="flat",
                    font=UI_FONT)
    style.map("Ghost.TButton",
              background=[("active", colors["surface"])],
              foreground=[("active", colors["text"])])

    for widget_style in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget_style,
                        background=colors["bg"],
                        foreground=colors["text"],
                        indicatorbackground=colors["bg_alt"],
                        indicatorforeground=colors["accent"],
                        bordercolor=colors["border"],
                        lightcolor=colors["border"],
                        darkcolor=colors["border"],
                        padding=3,
                        focuscolor=colors["bg"],
                        font=UI_FONT)
        style.map(widget_style,
                  background=[("active", colors["bg"])],
                  indicatorcolor=[("selected", colors["accent"]),
                                  ("!selected", colors["bg_alt"])],
                  indicatorbackground=[("active", colors["surface"]),
                                       ("selected", colors["accent"])])

    style.configure("TCombobox",
                    fieldbackground=colors["bg_alt"],
                    background=colors["surface"],
                    foreground=colors["text"],
                    bordercolor=colors["border"],
                    lightcolor=colors["border"],
                    darkcolor=colors["border"],
                    arrowcolor=colors["text_muted"],
                    padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", colors["bg_alt"])],
              foreground=[("readonly", colors["text"])],
              bordercolor=[("focus", colors["accent"]),
                           ("hover", colors["border_hi"])],
              arrowcolor=[("active", colors["accent"])])

    root.option_add("*TCombobox*Listbox.background",      colors["bg_alt"])
    root.option_add("*TCombobox*Listbox.foreground",      colors["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", colors["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", colors["accent_fg"])
    root.option_add("*TCombobox*Listbox.font",            UI_FONT)
    root.option_add("*TCombobox*Listbox.relief",          "flat")
    root.option_add("*TCombobox*Listbox.borderWidth",     0)

    style.configure("TProgressbar",
                    background=colors["accent"],
                    troughcolor=colors["bg_alt"],
                    bordercolor=colors["bg_alt"],
                    lightcolor=colors["accent"],
                    darkcolor=colors["accent"],
                    thickness=10)

    style.configure("Vertical.TScrollbar",
                    background=colors["surface"],
                    troughcolor=colors["bg"],
                    bordercolor=colors["bg"],
                    arrowcolor=colors["text_muted"],
                    lightcolor=colors["surface"],
                    darkcolor=colors["surface"],
                    relief="flat")
    style.map("Vertical.TScrollbar",
              background=[("active", colors["surface_hi"])],
              arrowcolor=[("active", colors["text"])])

    style.configure("TSeparator", background=colors["border"])


# =========================================================================== #
#  App                                                                         #
# =========================================================================== #
class FileOrganizerApp:
    # Severity levels for the structured logger.
    LVL_DEBUG, LVL_INFO, LVL_WARN, LVL_ERROR, LVL_SUCCESS, LVL_HEADER = (
        "DEBUG", "INFO", "WARN", "ERROR", "OK", "HEAD"
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("File Organizer")
        self.root.geometry("1140x800")
        self.root.minsize(1020, 720)

        self.theme_name = "dark"
        self.colors = THEMES[self.theme_name]

        # State -------------------------------------------------------- #
        self.source_path     = tk.StringVar()
        self.dest_path       = tk.StringVar()
        self.operation       = tk.StringVar(value="copy")
        self.run_mode        = tk.StringVar(value="transfer")
        self.dup_mode        = tk.StringVar(value="rename")
        self.verify_mode     = tk.StringVar(value="quick")
        self.include_subdirs = tk.BooleanVar(value=True)
        self.sync_writes     = tk.BooleanVar(value=True)
        self.verbose_log     = tk.BooleanVar(value=False)
        self.log_to_file     = tk.BooleanVar(value=False)
        self.sort_mode_label = tk.StringVar(value=SORT_LABEL_BY_KEY["year_month"])
        # Deliberately not persisted in profiles: a destructive toggle
        # should never be on because of a forgotten setting.
        self.format_source_after = tk.BooleanVar(value=False)
        self._format_eligible = False  # True only for regular transfer runs

        self._detect_job: Optional[str] = None
        self._auto_detected_key: Optional[str] = None
        self.usb_drives: list[UsbDrive] = []
        self._pending_restore: dict[str, dict] = {}

        self.scanned_files: list[Path] = []
        self.ext_counts: dict[str, int] = {}
        self.ext_sizes: dict[str, int] = {}
        self.type_vars: dict[str, tk.BooleanVar] = {}

        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()

        # File logging state
        self._logfile_path: Optional[Path] = None
        self._logfile_handle = None

        # Profiles ----------------------------------------------------- #
        self.profiles, last_used, self.last_locations = load_profiles_from_disk()
        self.current_profile = tk.StringVar(value=last_used or next(iter(self.profiles)))
        self._suppress_profile_apply = False  # internal flag

        apply_theme(self.root, self.colors)
        self._build_ui()
        self._apply_theme_to_non_ttk()
        self._configure_log_tags()

        # Apply the initial profile after UI is built
        try:
            self._apply_profile(self.current_profile.get(), include_paths=False)
        except Exception:
            pass

        self._on_run_mode_changed()
        self.dest_path.trace_add("write", self._schedule_detect)
        self.source_path.trace_add("write", lambda *_a: self._on_path_edited("source"))
        self.dest_path.trace_add("write", lambda *_a: self._on_path_edited("dest"))

        self._log_header(f"File Organizer started  •  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log_info(f"Pillow EXIF support: {'available' if PIL_AVAILABLE else 'NOT installed (pip install Pillow)'}",
                       level=self.LVL_INFO if PIL_AVAILABLE else self.LVL_WARN)
        self._log_debug(f"Profiles loaded from {PROFILES_FILE}: {len(self.profiles)} entries")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_drives(list_usb_drives())
        self._restore_locations()
        self.root.after(3000, self._poll_drives)
        self.root.after(100, self._poll_queue)

        self._omarchy_fingerprint = omarchy_theme_fingerprint()
        if self._omarchy_fingerprint is not None:
            if start_omarchy_watcher(
                    lambda: self.msg_queue.put(("omarchy_theme", None))):
                self._log_debug("Omarchy theme watcher active (inotify)")
            else:
                self.root.after(2000, self._poll_omarchy_theme)

    # ============================================================== #
    #  UI BUILD                                                       #
    # ============================================================== #
    def _build_ui(self) -> None:
        # ---- Header ------------------------------------------------ #
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 10, 16, 6))
        header.pack(fill=tk.X)

        ttk.Label(header, text="🗂  File Organizer", style="Title.TLabel").pack(side=tk.LEFT)
        if PIL_AVAILABLE:
            subtitle = "EXIF + MP4/MOV dates for photos & videos"
        else:
            subtitle = "Dates via file time  •  pip install Pillow for EXIF"
        ttk.Label(header, text=subtitle, style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(12, 0), pady=(6, 0))

        # Theme + profile cluster on the right
        self.theme_btn = ttk.Button(header, text="☀  Light",
                                    style="Ghost.TButton", command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=16)

        # ---- Profile bar ------------------------------------------- #
        profbar = ttk.Frame(self.root, padding=(16, 8, 16, 4))
        profbar.pack(fill=tk.X)
        ttk.Label(profbar, text="Profile:", style="Heading.TLabel").pack(side=tk.LEFT)
        self.profile_combo = ttk.Combobox(
            profbar, textvariable=self.current_profile,
            values=list(self.profiles.keys()),
            state="readonly", width=42)
        self.profile_combo.pack(side=tk.LEFT, padx=(8, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        ttk.Button(profbar, text="Save", command=self._save_current_profile)\
            .pack(side=tk.LEFT)
        ttk.Button(profbar, text="Save as…", command=self._save_profile_as)\
            .pack(side=tk.LEFT, padx=(6, 0))
        self.profile_delete_btn = ttk.Button(profbar, text="Delete",
                                             command=self._delete_current_profile)
        self.profile_delete_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.profile_status = ttk.Label(profbar, text="", style="Muted.TLabel")
        self.profile_status.pack(side=tk.LEFT, padx=(12, 0))

        # ---- Body -------------------------------------------------- #
        body = ttk.Frame(self.root, padding=(16, 6, 16, 6))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # Source + Destination on the same row, side-by-side.
        # -- Source ------------------------------------------------- #
        src = ttk.LabelFrame(body, text="  1.  Source  ", style="Card.TLabelframe")
        src.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))

        src_row = ttk.Frame(src)
        src_row.pack(fill=tk.X)
        ttk.Entry(src_row, textvariable=self.source_path).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(src_row, text="Browse…", command=self._browse_source).pack(side=tk.LEFT)

        src_usb = ttk.Frame(src)
        src_usb.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(src_usb, text="USB:", style="Heading.TLabel").pack(side=tk.LEFT)
        self.src_drive_combo = ttk.Combobox(src_usb, state="disabled")
        self.src_drive_combo.set("no USB drives detected")
        self.src_drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                  padx=(8, 6))
        self.src_drive_combo.bind("<<ComboboxSelected>>",
                                  lambda e: self._pick_drive("source"))
        ttk.Button(src_usb, text="⟳", width=3,
                   command=self._refresh_drives).pack(side=tk.LEFT)
        ttk.Button(src_usb, text="⏏", width=3,
                   command=lambda: self._eject_drive("source")
                   ).pack(side=tk.LEFT, padx=(4, 0))
        self.src_drive_hint = ttk.Label(src, text="", style="Muted.TLabel")
        self.src_drive_hint.pack(anchor="w", pady=(4, 0))

        src_row2 = ttk.Frame(src)
        src_row2.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(src_row2, text="Include subfolders",
                        variable=self.include_subdirs).pack(side=tk.LEFT)
        ttk.Button(src_row2, text="🔍  Scan", command=self._start_scan,
                   style="Accent.TButton").pack(side=tk.RIGHT)

        # -- Destination -------------------------------------------- #
        dst = ttk.LabelFrame(body, text="  2.  Destination  ", style="Card.TLabelframe")
        dst.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))

        dst_row = ttk.Frame(dst)
        dst_row.pack(fill=tk.X)
        ttk.Entry(dst_row, textvariable=self.dest_path).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(dst_row, text="Browse…", command=self._browse_dest).pack(side=tk.LEFT)

        dst_usb = ttk.Frame(dst)
        dst_usb.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(dst_usb, text="USB:", style="Heading.TLabel").pack(side=tk.LEFT)
        self.dst_drive_combo = ttk.Combobox(dst_usb, state="disabled")
        self.dst_drive_combo.set("no USB drives detected")
        self.dst_drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                  padx=(8, 6))
        self.dst_drive_combo.bind("<<ComboboxSelected>>",
                                  lambda e: self._pick_drive("dest"))
        ttk.Button(dst_usb, text="⟳", width=3,
                   command=self._refresh_drives).pack(side=tk.LEFT)
        ttk.Button(dst_usb, text="⏏", width=3,
                   command=lambda: self._eject_drive("dest")
                   ).pack(side=tk.LEFT, padx=(4, 0))
        self.dst_drive_hint = ttk.Label(dst, text="", style="Muted.TLabel")
        self.dst_drive_hint.pack(anchor="w", pady=(4, 0))

        dst_row2 = ttk.Frame(dst)
        dst_row2.pack(fill=tk.X, pady=(8, 0))
        self.detect_label = ttk.Label(dst_row2, text="", style="Hint.TLabel")
        self.detect_label.pack(side=tk.LEFT)
        ttk.Button(dst_row2, text="Auto-detect",
                   command=self._detect_now).pack(side=tk.RIGHT)

        # -- File types -------------------------------------------- #
        types = ttk.LabelFrame(body, text="  3.  File types found  ",
                               style="Card.TLabelframe")
        types.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 8))

        ctrl_row = ttk.Frame(types)
        ctrl_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(ctrl_row, text="Select all",
                   command=lambda: self._toggle_all_types(True)).pack(side=tk.LEFT)
        ttk.Button(ctrl_row, text="Select none",
                   command=lambda: self._toggle_all_types(False)).pack(side=tk.LEFT, padx=(6, 0))
        self.types_summary = ttk.Label(ctrl_row, text="Press Scan to populate.",
                                       style="Muted.TLabel")
        self.types_summary.pack(side=tk.LEFT, padx=(12, 0))

        canvas_wrap = ttk.Frame(types)
        canvas_wrap.pack(fill=tk.BOTH, expand=True)
        self.types_canvas = tk.Canvas(canvas_wrap, height=80,
                                      highlightthickness=0, bd=0)
        yscroll = ttk.Scrollbar(canvas_wrap, orient=tk.VERTICAL,
                                command=self.types_canvas.yview)
        self.types_inner = ttk.Frame(self.types_canvas)
        self.types_inner.bind(
            "<Configure>",
            lambda e: self.types_canvas.configure(scrollregion=self.types_canvas.bbox("all")),
        )
        self._types_window = self.types_canvas.create_window((0, 0),
                                                             window=self.types_inner,
                                                             anchor="nw")
        self.types_canvas.configure(yscrollcommand=yscroll.set)
        self.types_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        # -- Options (compact 2-column grid) ------------------------ #
        opts = ttk.LabelFrame(body, text="  4.  Options  ", style="Card.TLabelframe")
        opts.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 8))

        grid = ttk.Frame(opts)
        grid.pack(fill=tk.X)
        # Two label/value column pairs
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        # Row 0: Mode (left)  |  Operation (right)
        ttk.Label(grid, text="Mode", style="Heading.TLabel")\
            .grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        mode_row = ttk.Frame(grid)
        mode_row.grid(row=0, column=1, sticky="w", pady=2)
        for key, label in RUN_MODES:
            ttk.Radiobutton(mode_row, text=label,
                            variable=self.run_mode, value=key,
                            command=self._on_run_mode_changed)\
                .pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(grid, text="Operation", style="Heading.TLabel")\
            .grid(row=0, column=2, sticky="w", padx=(16, 8), pady=2)
        op_row = ttk.Frame(grid)
        op_row.grid(row=0, column=3, sticky="w", pady=2)
        self.copy_rb = ttk.Radiobutton(op_row, text="Copy (keep originals)",
                                       variable=self.operation, value="copy")
        self.copy_rb.pack(side=tk.LEFT)
        self.move_rb = ttk.Radiobutton(op_row, text="Move (delete originals)",
                                       variable=self.operation, value="move")
        self.move_rb.pack(side=tk.LEFT, padx=(12, 0))

        # Row 1: Sort  (spans both columns of values for the long combobox)
        ttk.Label(grid, text="Organize into", style="Heading.TLabel")\
            .grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.sort_combo = ttk.Combobox(grid, textvariable=self.sort_mode_label,
                                       values=[lbl for _, lbl in SORT_MODES],
                                       state="readonly")
        self.sort_combo.grid(row=1, column=1, columnspan=3, sticky="we", pady=4)

        # Row 2: Duplicates (left) | Verify (right)
        ttk.Label(grid, text="If duplicate", style="Heading.TLabel")\
            .grid(row=2, column=0, sticky="w", padx=(0, 8), pady=2)
        dup_row = ttk.Frame(grid)
        dup_row.grid(row=2, column=1, sticky="w", pady=2)
        self.dup_radios = []
        for key, label in DUP_MODES:
            rb = ttk.Radiobutton(dup_row, text=label,
                                 variable=self.dup_mode, value=key)
            rb.pack(side=tk.LEFT, padx=(0, 10))
            self.dup_radios.append(rb)

        ttk.Label(grid, text="Double-check", style="Heading.TLabel")\
            .grid(row=2, column=2, sticky="w", padx=(16, 8), pady=2)
        ver_row = ttk.Frame(grid)
        ver_row.grid(row=2, column=3, sticky="w", pady=2)
        for key, label in VERIFY_MODES:
            ttk.Radiobutton(ver_row, text=label,
                            variable=self.verify_mode, value=key)\
                .pack(side=tk.LEFT, padx=(0, 10))

        # Row 3: After-transfer actions
        ttk.Label(grid, text="After transfer", style="Heading.TLabel")\
            .grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(6, 2))
        after_row = ttk.Frame(grid)
        after_row.grid(row=3, column=1, columnspan=3, sticky="w", pady=(6, 2))
        self.format_check = ttk.Checkbutton(
            after_row,
            text="Format source drive when finished (asks again — erases the whole drive)",
            variable=self.format_source_after)
        self.format_check.pack(side=tk.LEFT)

        # Row 4: Logging toggles
        ttk.Label(grid, text="Logging", style="Heading.TLabel")\
            .grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(6, 2))
        log_row = ttk.Frame(grid)
        log_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Checkbutton(log_row, text="Verbose (log every file)",
                        variable=self.verbose_log).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Checkbutton(log_row, text="Save log to file in destination",
                        variable=self.log_to_file).pack(side=tk.LEFT)
        ttk.Checkbutton(log_row, text="Sync writes to device (recommended for USB)",
                        variable=self.sync_writes).pack(side=tk.LEFT, padx=(16, 0))

        # -- Actions ----------------------------------------------- #
        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.start_btn = ttk.Button(actions, text="▶  Start transfer",
                                    command=self._start_run, style="Accent.TButton")
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(actions, text="■  Cancel",
                                     command=self._cancel_run,
                                     state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="🗑  Clear log",
                   command=self._clear_log,
                   style="Ghost.TButton").pack(side=tk.RIGHT)

        # -- Progress + Log share remaining vertical space --------- #
        body.rowconfigure(4, weight=0)
        body.rowconfigure(5, weight=1)

        prog = ttk.LabelFrame(body, text="  Progress  ", style="Card.TLabelframe")
        prog.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 4))
        self.progress_label = ttk.Label(prog, text="Ready.", style="Muted.TLabel")
        self.progress_label.pack(fill=tk.X)

        log_box = ttk.LabelFrame(body, text="  Log  ", style="Card.TLabelframe")
        log_box.grid(row=5, column=0, columnspan=2, sticky="nsew")
        self.log = scrolledtext.ScrolledText(log_box, height=8, wrap=tk.WORD,
                                             state=tk.DISABLED, relief="flat",
                                             bd=0, highlightthickness=0)
        self.log.pack(fill=tk.BOTH, expand=True)

    # -------- re-style non-ttk widgets (Text, Canvas) on theme changes --- #
    def _apply_theme_to_non_ttk(self) -> None:
        c = self.colors
        self.log.configure(bg=c["bg_alt"], fg=c["text"],
                           insertbackground=c["text"],
                           selectbackground=c["accent"],
                           selectforeground=c["accent_fg"],
                           font=UI_FONT_MONO)
        self.types_canvas.configure(bg=c["bg"])
        self.root.configure(bg=c["bg"])

    def _configure_log_tags(self) -> None:
        c = self.colors
        # Each level gets its own color tag.
        self.log.tag_config("DEBUG",  foreground=c["debug"])
        self.log.tag_config("INFO",   foreground=c["text"])
        self.log.tag_config("WARN",   foreground=c["warning"])
        self.log.tag_config("ERROR",  foreground=c["error"])
        self.log.tag_config("OK",     foreground=c["success"])
        self.log.tag_config("HEAD",   foreground=c["accent"], font=UI_FONT_BOLD)
        self.log.tag_config("MUTED",  foreground=c["text_muted"])

    # ---------------- Theme toggle ---------------- #
    def _toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.colors = THEMES[self.theme_name]
        apply_theme(self.root, self.colors)
        self._apply_theme_to_non_ttk()
        self._configure_log_tags()
        if self.theme_name == "dark":
            self.theme_btn.configure(text="☀  Light")
        else:
            self.theme_btn.configure(text="🌙  Dark")

    def _recheck_omarchy_theme(self) -> None:
        """Re-style live when the active Omarchy theme changed on disk."""
        try:
            fp = omarchy_theme_fingerprint()
            if fp is not None and fp != self._omarchy_fingerprint:
                self._omarchy_fingerprint = fp
                palette = load_omarchy_palette()
                if palette is not None:
                    THEMES["dark"] = palette
                    if self.theme_name == "dark":
                        self.colors = palette
                        apply_theme(self.root, self.colors)
                        self._apply_theme_to_non_ttk()
                        self._configure_log_tags()
        except Exception:
            pass  # never let theme rechecks break the app

    def _poll_omarchy_theme(self) -> None:
        """Fallback when inotify is unavailable: check every 2 s."""
        self._recheck_omarchy_theme()
        self.root.after(2000, self._poll_omarchy_theme)

    # ============================================================== #
    #  PROFILES                                                      #
    # ============================================================== #
    def _capture_profile(self, include_paths: bool = True) -> dict:
        """Snapshot current settings into a profile dict."""
        sort_key = SORT_KEY_BY_LABEL.get(self.sort_mode_label.get(), "year_month")
        data = {
            "operation":       self.operation.get(),
            "run_mode":        self.run_mode.get(),
            "sort_mode":       sort_key,
            "dup_mode":        self.dup_mode.get(),
            "verify_mode":     self.verify_mode.get(),
            "include_subdirs": bool(self.include_subdirs.get()),
            "sync_writes":     bool(self.sync_writes.get()),
            "verbose_log":     bool(self.verbose_log.get()),
            "log_to_file":     bool(self.log_to_file.get()),
            "theme":           self.theme_name,
        }
        if include_paths:
            data["source_path"] = self.source_path.get()
            data["dest_path"]   = self.dest_path.get()
        return data

    def _apply_profile(self, name: str, include_paths: bool = True) -> None:
        """Apply the named profile to the UI state."""
        if name not in self.profiles:
            return
        data = self.profiles[name]
        self._suppress_profile_apply = True
        try:
            if "operation"       in data: self.operation.set(data["operation"])
            if "run_mode"        in data: self.run_mode.set(data["run_mode"])
            if "sort_mode"       in data:
                lbl = SORT_LABEL_BY_KEY.get(data["sort_mode"])
                if lbl:
                    self.sort_mode_label.set(lbl)
            if "dup_mode"        in data: self.dup_mode.set(data["dup_mode"])
            if "verify_mode"     in data: self.verify_mode.set(data["verify_mode"])
            if "include_subdirs" in data: self.include_subdirs.set(bool(data["include_subdirs"]))
            if "sync_writes"     in data: self.sync_writes.set(bool(data["sync_writes"]))
            if "verbose_log"     in data: self.verbose_log.set(bool(data["verbose_log"]))
            if "log_to_file"     in data: self.log_to_file.set(bool(data["log_to_file"]))
            if include_paths:
                if "source_path" in data: self.source_path.set(data.get("source_path", ""))
                if "dest_path"   in data: self.dest_path.set(data.get("dest_path", ""))
            if "theme" in data and data["theme"] in THEMES and data["theme"] != self.theme_name:
                self._toggle_theme()
        finally:
            self._suppress_profile_apply = False
        self._on_run_mode_changed()
        self._refresh_profile_status(name)
        self._log_info(f"Applied profile: {name}", level=self.LVL_INFO)

    def _on_profile_selected(self, _evt=None) -> None:
        if self._suppress_profile_apply:
            return
        name = self.current_profile.get()
        # When the user picks from the dropdown, restore everything (no paths
        # by default — most users don't want their paths overridden).
        self._apply_profile(name, include_paths=False)

    def _refresh_profile_status(self, name: str) -> None:
        is_default = name in DEFAULT_PROFILES
        if is_default:
            self.profile_status.configure(text="(built-in)")
            self.profile_delete_btn.configure(state=tk.DISABLED)
        else:
            self.profile_status.configure(text="(custom)")
            self.profile_delete_btn.configure(state=tk.NORMAL)

    def _save_current_profile(self) -> None:
        name = self.current_profile.get()
        if not name:
            return
        if name in DEFAULT_PROFILES:
            ans = messagebox.askyesno(
                "Overwrite built-in?",
                f"'{name}' is a built-in profile. Save your current settings as an "
                f"override of this name? (You can restore the original by deleting "
                f"the override.)")
            if not ans:
                return
        data = self._capture_profile(include_paths=True)
        self.profiles[name] = data
        err = save_profiles_to_disk(self.profiles, name, self.last_locations)
        if err:
            self._log_error(f"Could not save profiles: {err}")
            messagebox.showerror("Save profile", f"Could not save:\n{err}")
            return
        self._refresh_profile_status(name)
        self._log_success(f"Saved profile: {name}")

    def _save_profile_as(self) -> None:
        name = simpledialog.askstring("Save profile as",
                                      "Name for the new profile:",
                                      parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.profiles:
            ans = messagebox.askyesno("Overwrite?",
                                      f"A profile named '{name}' already exists. "
                                      f"Overwrite it?")
            if not ans:
                return
        data = self._capture_profile(include_paths=True)
        self.profiles[name] = data
        # Refresh the dropdown values
        self.profile_combo.configure(values=list(self.profiles.keys()))
        self.current_profile.set(name)
        err = save_profiles_to_disk(self.profiles, name, self.last_locations)
        if err:
            self._log_error(f"Could not save profiles: {err}")
            messagebox.showerror("Save profile", f"Could not save:\n{err}")
            return
        self._refresh_profile_status(name)
        self._log_success(f"Saved new profile: {name}")

    def _delete_current_profile(self) -> None:
        name = self.current_profile.get()
        if not name or name in DEFAULT_PROFILES:
            return
        ans = messagebox.askyesno("Delete profile",
                                  f"Delete profile '{name}'?")
        if not ans:
            return
        self.profiles.pop(name, None)
        # Drop back to the first available profile
        new_name = next(iter(self.profiles))
        self.profile_combo.configure(values=list(self.profiles.keys()))
        self.current_profile.set(new_name)
        err = save_profiles_to_disk(self.profiles, new_name, self.last_locations)
        if err:
            self._log_error(f"Could not save profiles: {err}")
        self._refresh_profile_status(new_name)
        self._apply_profile(new_name, include_paths=False)
        self._log_info(f"Deleted profile: {name}")

    # ============================================================== #
    #  SOURCE / DESTINATION                                          #
    # ============================================================== #
    def _browse_source(self) -> None:
        p = filedialog.askdirectory(title="Select source drive or folder",
                                    initialdir=default_source_dir())
        if p:
            self.source_path.set(p)
            self._log_debug(f"Source set to: {p}")

    def _browse_dest(self) -> None:
        p = filedialog.askdirectory(title="Select destination drive or folder",
                                    initialdir=default_dest_dir())
        if p:
            self.dest_path.set(p)
            self._log_debug(f"Destination set to: {p}")

    # ---------------- USB drive picker ---------------- #
    def _refresh_drives(self) -> None:
        """List drives off the UI thread; result arrives via the queue."""
        threading.Thread(
            target=lambda: self.msg_queue.put(("drives", list_usb_drives())),
            daemon=True).start()

    def _poll_drives(self) -> None:
        self._refresh_drives()
        self.root.after(3000, self._poll_drives)

    @staticmethod
    def _drive_display(d: UsbDrive) -> str:
        if not d.mounted:
            return f"{d.label} — {human_size(d.size)} (not mounted)"
        return f"{d.label} — {human_size(d.size)} ({human_size(d.free)} free)"

    @staticmethod
    def _drive_ident(d: UsbDrive) -> str:
        """Selection identity that survives mounting/unmounting."""
        return f"dev:{d.device}" if d.device else d.mountpoint

    def _update_drives(self, drives: list[UsbDrive]) -> None:
        key = [(d.label, d.mountpoint, d.free, d.device) for d in drives]
        old_key = [(d.label, d.mountpoint, d.free, d.device)
                   for d in self.usb_drives]
        old_drives = self.usb_drives
        self.usb_drives = drives
        if key == old_key:
            return
        values = [self._drive_display(d) for d in drives]
        idents = [self._drive_ident(d) for d in drives]
        for combo in (self.src_drive_combo, self.dst_drive_combo):
            old_idx = combo.current()
            old_ident = (self._drive_ident(old_drives[old_idx])
                         if 0 <= old_idx < len(old_drives) else None)
            combo.configure(values=values)
            if values:
                combo.configure(state="readonly")
                if old_ident in idents:
                    combo.current(idents.index(old_ident))
                else:
                    combo.set("")
            else:
                combo.set("no USB drives detected")
                combo.configure(state="disabled")
        self._try_pending_restore()

    def _pick_drive(self, kind: str) -> None:
        combo = self.src_drive_combo if kind == "source" else self.dst_drive_combo
        idx = combo.current()
        if idx < 0 or idx >= len(self.usb_drives):
            return
        drive = self.usb_drives[idx]
        var = self.source_path if kind == "source" else self.dest_path
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        if not drive.mounted:
            self._mount_and_pick(kind, drive)
            return
        var.set(drive.mountpoint)
        hint.configure(text="")
        self._log_debug(f"{kind} set from USB picker: {drive.mountpoint}")

    # ---------------- Mount / unmount ---------------- #
    def _mount_and_pick(self, kind: str, drive: UsbDrive) -> None:
        """Mount an unmounted pick in the background, then fill the path."""
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        if not drive.device:
            hint.configure(text=f"Can't mount {drive.label} automatically.",
                           foreground=self.colors["warning"])
            return
        hint.configure(text=f"Mounting {drive.label}…",
                       foreground=self.colors["text_muted"])
        self._log_info(f"Mounting {drive.label} (/dev/{drive.device})…")
        threading.Thread(
            target=lambda: self.msg_queue.put(
                ("mount_result", (kind, drive, *mount_drive(drive.device)))),
            daemon=True).start()

    def _eject_drive(self, kind: str) -> None:
        """Safely unmount the drive selected in this side's picker."""
        combo = self.src_drive_combo if kind == "source" else self.dst_drive_combo
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        idx = combo.current()
        if idx < 0 or idx >= len(self.usb_drives):
            return
        drive = self.usb_drives[idx]
        if not drive.mounted:
            hint.configure(text=f"{drive.label} is not mounted.",
                           foreground=self.colors["text_muted"])
            return
        if not drive.device:
            hint.configure(text=f"Can't unmount {drive.label} automatically.",
                           foreground=self.colors["warning"])
            return
        if self.worker is not None and self.worker.is_alive():
            hint.configure(text="Finish or cancel the transfer first.",
                           foreground=self.colors["warning"])
            return
        self._release_drive_holds(drive)
        hint.configure(text=f"Unmounting {drive.label}…",
                       foreground=self.colors["text_muted"])
        self._log_info(f"Unmounting {drive.label} (/dev/{drive.device})…")
        threading.Thread(
            target=lambda: self.msg_queue.put(
                ("unmount_result",
                 (kind, drive, *unmount_drive_with_diagnosis(
                     drive.device, drive.mountpoint)))),
            daemon=True).start()

    def _release_drive_holds(self, drive: UsbDrive) -> None:
        """Let go of anything this app itself holds on the drive, so an
        unmount isn't blocked by our own log file or working directory."""
        root = drive.mountpoint.rstrip("/")
        prefix = root + "/"
        if (self._logfile_handle is not None
                and self._logfile_path is not None
                and str(self._logfile_path).startswith(prefix)):
            self._log_info(f"Closing session log on {drive.label} "
                           f"before unmounting.")
            self._maybe_close_file_log()
        try:
            cwd = os.getcwd()
            if cwd == root or cwd.startswith(prefix):
                os.chdir(Path.home())
                self._log_debug("Moved working directory off the drive.")
        except OSError:
            pass

    def _handle_mount_result(self, payload) -> None:
        kind, drive, ok, detail = payload
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        if ok:
            var = self.source_path if kind == "source" else self.dest_path
            var.set(detail)
            hint.configure(text=f"✓ {drive.label} mounted",
                           foreground=self.colors["success"])
            self._log_info(f"Mounted {drive.label} at {detail}")
        else:
            hint.configure(text=f"Mount failed: {drive.label}",
                           foreground=self.colors["error"])
            self._log_error(f"Mount of {drive.label} failed: {detail}")
        self._refresh_drives()

    def _source_usb_drive(self) -> Optional[UsbDrive]:
        """The mounted USB drive the current source path lives on, if any."""
        src = self.source_path.get().strip()
        if not src:
            return None
        try:
            p = Path(src).resolve()
        except OSError:
            return None
        best: Optional[UsbDrive] = None
        best_len = -1
        for d in self.usb_drives:
            if not d.mounted:
                continue
            try:
                mp = Path(d.mountpoint).resolve()
                p.relative_to(mp)
            except (OSError, ValueError):
                continue
            if len(mp.as_posix()) > best_len:
                best, best_len = d, len(mp.as_posix())
        return best

    def _maybe_format_source(self, errors: int, verify_fail: int) -> None:
        """After a transfer run: offer to format the source drive if the
        option is on and the run finished clean. Called on the UI thread."""
        if not self.format_source_after.get() or not self._format_eligible:
            return
        if self.cancel_event.is_set():
            self._log_warn("Format source skipped: the run was cancelled.")
            return
        if errors or verify_fail:
            self._log_warn("Format source skipped: the run finished with errors.")
            return
        drive = self._source_usb_drive()
        if drive is None:
            self._log_warn("Format source skipped: source is not on a "
                           "mounted USB drive.")
            return
        if not drive.device or not drive.fstype:
            self._log_warn(f"Format source skipped: unknown device or "
                           f"filesystem for '{drive.label}'.")
            return
        confirm = messagebox.askyesno(
            "Format source drive?",
            f"ERASE EVERYTHING on '{drive.label}'?\n\n"
            f"Device:      /dev/{drive.device}\n"
            f"Filesystem:  {drive.fstype}\n"
            f"Size:        {human_size(drive.size)}\n"
            f"Mounted at:  {drive.mountpoint}\n\n"
            f"The WHOLE drive is formatted, not just the transferred "
            f"folder.\nThis cannot be undone.",
            icon="warning", default="no")
        if not confirm:
            self._log_info("Format source declined.")
            return
        self._log_warn(f"Formatting {drive.label} "
                       f"(/dev/{drive.device}, {drive.fstype})…")
        self._release_drive_holds(drive)
        self._set_controls_running(True)
        self.worker = threading.Thread(target=self._format_source_worker,
                                       args=(drive,), daemon=True)
        self.worker.start()

    def _format_source_worker(self, drive: UsbDrive) -> None:
        ok, msg = unmount_drive_with_diagnosis(drive.device, drive.mountpoint)
        if not ok:
            self.msg_queue.put(("format_result",
                                (drive, False, None, f"unmount failed: {msg}")))
            return
        ok, msg = format_drive(drive.device, drive.fstype, drive.label)
        if not ok:
            self.msg_queue.put(("format_result", (drive, False, None, msg)))
            return
        mounted, detail = mount_drive(drive.device)
        self.msg_queue.put(("format_result",
                            (drive, True,
                             detail if mounted else None,
                             "" if mounted else detail)))

    def _handle_format_result(self, payload) -> None:
        drive, ok, mountpoint, detail = payload
        self._set_controls_running(False)
        if ok:
            self._log_success(f"✓ Formatted {drive.label} — source drive "
                              f"is now empty.")
            # The scan list points at erased files now.
            self.scanned_files = []
            if mountpoint:
                self.source_path.set(mountpoint)
            elif detail:
                self._log_warn(f"Formatted, but remount of {drive.label} "
                               f"failed: {detail}")
        else:
            self._log_error(f"Format of {drive.label} failed: {detail}")
            messagebox.showerror(
                "Format failed",
                f"Could not format '{drive.label}':\n{detail}\n\n"
                f"No files were harmed if formatting never started; the "
                f"drive may just need to be remounted (⟳, then pick it).")
        self._refresh_drives()

    def _handle_unmount_result(self, payload) -> None:
        kind, drive, ok, detail = payload
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        if ok:
            hint.configure(text=f"⏏ {drive.label} unmounted — safe to remove",
                           foreground=self.colors["success"])
            self._log_info(f"Unmounted {drive.label}")
            # Clear paths that pointed at the now-gone mountpoint.
            root = drive.mountpoint.rstrip("/")
            for var in (self.source_path, self.dest_path):
                p = var.get().strip()
                if p and (p == drive.mountpoint or p == root
                          or p.startswith(root + "/")):
                    var.set("")
        else:
            hint.configure(text=f"Unmount failed: {drive.label}",
                           foreground=self.colors["error"])
            self._log_error(f"Unmount of {drive.label} failed: {detail}")
        self._refresh_drives()

    # ---------------- Location memory ---------------- #
    def _restore_locations(self) -> None:
        """Restore last-used source/dest; called once at startup with a
        fresh synchronous drive listing already in self.usb_drives."""
        for kind, var, hint in (
                ("source", self.source_path, self.src_drive_hint),
                ("dest",   self.dest_path,   self.dst_drive_hint)):
            record = self.last_locations.get(kind)
            path, missing = resolve_location(record, self.usb_drives)
            if path:
                var.set(path)
                self._log_debug(f"Restored last {kind}: {path}")
            elif missing:
                self._pending_restore[kind] = record
                hint.configure(text=f"Last used drive '{missing}' not connected.",
                               foreground=self.colors["text_muted"])

    def _try_pending_restore(self) -> None:
        """Auto-fill a remembered location once its drive reappears."""
        for kind in list(self._pending_restore):
            record = self._pending_restore[kind]
            path, _missing = resolve_location(record, self.usb_drives)
            if not path:
                continue
            del self._pending_restore[kind]
            var = self.source_path if kind == "source" else self.dest_path
            hint = (self.src_drive_hint if kind == "source"
                    else self.dst_drive_hint)
            var.set(path)
            label = record.get("label", "")
            hint.configure(text=f"✓ {label} reconnected",
                           foreground=self.colors["success"])
            self._log_info(f"Drive '{label}' reconnected — {kind} restored: {path}")

    def _on_path_edited(self, kind: str) -> None:
        """Any write to a path var cancels that side's pending restore.

        Programmatic writes are safe: a pending entry only exists while the
        remembered drive is disconnected, and _try_pending_restore removes
        the entry before it sets the variable.
        """
        if self._pending_restore.pop(kind, None) is not None:
            hint = (self.src_drive_hint if kind == "source"
                    else self.dst_drive_hint)
            hint.configure(text="")

    def _record_locations(self) -> None:
        """Snapshot current paths into self.last_locations (not yet saved)."""
        for kind, var in (("source", self.source_path),
                          ("dest", self.dest_path)):
            path = var.get().strip()
            if not path:
                continue
            record = make_location_record(path, self.usb_drives)
            if "label" not in record and not Path(path).is_dir():
                # Drive unplugged since the path was set: the old record
                # (which may still restore by label) is worth more.
                continue
            self.last_locations[kind] = record

    # ---------------- Auto-detection ---------------- #
    def _schedule_detect(self, *_args) -> None:
        if self._detect_job is not None:
            try:
                self.root.after_cancel(self._detect_job)
            except Exception:
                pass
        self._detect_job = self.root.after(400, self._detect_now)

    def _detect_now(self) -> None:
        self._detect_job = None
        path = self.dest_path.get().strip()
        if not path:
            self.detect_label.configure(text="", foreground=self.colors["success"])
            return
        p = Path(path)
        if not p.exists() or not p.is_dir():
            self.detect_label.configure(
                text="Destination does not exist yet — it will be created.",
                foreground=self.colors["text_muted"])
            return

        key = detect_folder_structure(p)
        if key is None:
            self.detect_label.configure(
                text="No existing folder structure detected.",
                foreground=self.colors["text_muted"])
            self._auto_detected_key = None
            return

        label = SORT_LABEL_BY_KEY[key]
        self.sort_mode_label.set(label)
        self._auto_detected_key = key
        self.detect_label.configure(
            text=f"✓  Detected — auto-selected: {label.strip()}",
            foreground=self.colors["success"])
        self._log_debug(f"Auto-detected structure '{key}' in {p}")

    # ============================================================== #
    #  SCANNING                                                      #
    # ============================================================== #
    def _start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "An operation is already running.")
            return
        src = self.source_path.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("Invalid source",
                                 "Please choose a valid source folder or drive.")
            return

        self._set_controls_running(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.progress_label.configure(
            text="Scanning… this can take a moment on large drives.")
        self._log_header(f"SCAN  •  {src}  •  recurse={self.include_subdirs.get()}")

        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._scan_worker,
            args=(Path(src), self.include_subdirs.get()),
            daemon=True)
        self.worker.start()

    def _scan_worker(self, source: Path, recurse: bool) -> None:
        counts: dict[str, int] = defaultdict(int)
        sizes:  dict[str, int] = defaultdict(int)
        files: list[Path] = []
        t0 = time.time()
        try:
            for p in walk_files(source, recurse, self.cancel_event):
                files.append(p)
                ext = p.suffix.lower()
                counts[ext] += 1
                try:
                    sizes[ext] += p.stat().st_size
                except OSError:
                    pass
                if len(files) % 500 == 0:
                    self.msg_queue.put(("scan_progress", len(files)))
            elapsed = time.time() - t0
            self.msg_queue.put(("scan_done", (files, dict(counts), dict(sizes), elapsed)))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("scan_error", (str(e), tb)))

    def _finalize_scan(self, files, counts, sizes, elapsed) -> None:
        self.scanned_files = files
        self.ext_counts = counts
        self.ext_sizes = sizes
        self._rebuild_type_checkboxes()
        total = len(files)
        total_size = sum(sizes.values())
        self.types_summary.configure(
            text=f"{total:,} files  •  {human_size(total_size)}  •  {len(counts)} file types")
        self._log_success(
            f"Scan complete: {total:,} files, {human_size(total_size)} "
            f"across {len(counts)} types in {human_duration(elapsed)}.")
        # Per-type breakdown (verbose only or always for small counts)
        if total > 0:
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            for ext, n in top[:10]:
                self._log_debug(
                    f"    .{ext.lstrip('.'):<6} {n:>7,} files   {human_size(sizes.get(ext, 0))}")
            if len(top) > 10:
                self._log_debug(f"    … and {len(top) - 10} more types")

    def _rebuild_type_checkboxes(self) -> None:
        for child in self.types_inner.winfo_children():
            child.destroy()
        self.type_vars.clear()

        if not self.ext_counts:
            ttk.Label(self.types_inner, text="No files found.",
                      style="Muted.TLabel").pack(anchor="w")
            return

        entries = sorted(self.ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        cols = 4  # tighter grid
        for i, (ext, count) in enumerate(entries):
            size = self.ext_sizes.get(ext, 0)
            var = tk.BooleanVar(value=True)
            self.type_vars[ext] = var
            label = f"{ext.lstrip('.').upper():<5}  {count:>6,}  ({human_size(size)})"
            cb = ttk.Checkbutton(self.types_inner, text=label, variable=var)
            r, c = divmod(i, cols)
            cb.grid(row=r, column=c, sticky="w", padx=10, pady=2)
        for c in range(cols):
            self.types_inner.columnconfigure(c, weight=1)

    def _toggle_all_types(self, value: bool) -> None:
        for var in self.type_vars.values():
            var.set(value)

    # ============================================================== #
    #  TRANSFER / VERIFY                                             #
    # ============================================================== #
    def _on_run_mode_changed(self) -> None:
        mode = self.run_mode.get()
        transfer_involved = mode in ("transfer", "transfer_verify")
        state = tk.NORMAL if transfer_involved else tk.DISABLED
        for w in (self.copy_rb, self.move_rb, self.format_check,
                  *self.dup_radios):
            try:
                w.configure(state=state)
            except tk.TclError:
                pass
        if mode == "verify":
            self.start_btn.configure(text="🔎  Start verify")
        elif mode == "transfer_verify":
            self.start_btn.configure(text="▶  Transfer + verify")
        else:
            self.start_btn.configure(text="▶  Start transfer")
        if mode == "verify" and self.verify_mode.get() == "off":
            self.verify_mode.set("quick")

    def _start_run(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "An operation is already running.")
            return
        if not self.scanned_files:
            messagebox.showerror("Nothing to do", "Scan the source first.")
            return
        dest = self.dest_path.get().strip()
        if not dest:
            messagebox.showerror("Destination",
                                 "Please choose a destination folder.")
            return
        dest_path = Path(dest)

        mode = self.run_mode.get()

        if mode == "verify":
            if not dest_path.exists() or not dest_path.is_dir():
                messagebox.showerror("Destination",
                                     "Destination does not exist — nothing to verify.")
                return
        else:
            try:
                dest_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Destination error",
                                     f"Cannot create destination: {e}")
                return

        try:
            if dest_path.resolve() == Path(self.source_path.get()).resolve():
                messagebox.showerror("Invalid",
                                     "Source and destination are the same folder.")
                return
        except Exception:
            pass

        selected_exts = {ext for ext, var in self.type_vars.items() if var.get()}
        if not selected_exts:
            messagebox.showerror("No file types",
                                 "Please select at least one file type.")
            return

        files_to_process = [p for p in self.scanned_files
                            if p.suffix.lower() in selected_exts]
        if not files_to_process:
            messagebox.showinfo("Nothing to do",
                                "No files match the selected types.")
            return
        self._record_locations()
        save_profiles_to_disk(self.profiles, self.current_profile.get(),
                              self.last_locations)
        total_size = sum(safe_size(p) for p in files_to_process)

        sort_label = self.sort_mode_label.get()
        sort_key = SORT_KEY_BY_LABEL.get(sort_label, "none")
        verify = self.verify_mode.get()
        if mode == "verify" and verify == "off":
            verify = "quick"

        # Open the file logger if requested
        self._maybe_open_file_log(dest_path, mode)

        # --- Verify-only branch --- #
        if mode == "verify":
            confirm = messagebox.askyesno(
                "Confirm verify (read-only)",
                f"Verify {len(files_to_process):,} source file(s) "
                f"({human_size(total_size)}) against:\n  {dest_path}\n\n"
                f"Sort mode:     {sort_label.strip()}\n"
                f"Double-check:  {dict(VERIFY_MODES)[verify]}\n\n"
                f"This is read-only — no files will be deleted or modified.\n"
                f"Proceed?"
            )
            if not confirm:
                return
            self._set_controls_running(True)
            self.progress.stop()
            self.progress.configure(mode="determinate",
                                    maximum=len(files_to_process), value=0)
            self.progress_label.configure(
                text=f"Verifying…  0 / {len(files_to_process):,}")
            self._log_header(
                f"VERIFY  •  {len(files_to_process):,} files ({human_size(total_size)})  →  {dest_path}")
            self._log_info(
                f"sort: {sort_label.strip()}  •  check: {dict(VERIFY_MODES)[verify]}  •  read-only")
            self.cancel_event.clear()
            self.worker = threading.Thread(
                target=self._verify_worker,
                args=(files_to_process, dest_path, sort_key, verify),
                daemon=True)
            self.worker.start()
            return

        # --- Transfer (with or without verify) branch --- #
        op = self.operation.get()
        dup = self.dup_mode.get()
        effective_verify = verify if mode == "transfer_verify" else "off"
        verb = "Moving" if op == "move" else "Copying"

        warning = ""
        if op == "move":
            warning = ("\n\nNote: Move will delete originals from the source. "
                       "If 'Double-check' is on, each source is only deleted "
                       "AFTER its copy is verified.")
        if dup == "erase":
            check = ("SHA-256" if effective_verify == "hash"
                     else "size + mtime")
            warning += ("\n\nNote: 'Erase source' deletes a source file when "
                        f"an identical copy ({check} match) already exists "
                        "at the destination. Same-name files with different "
                        "content are renamed and kept.")
        if self.format_source_after.get():
            warning += ("\n\n⚠ Format source is ON — after a clean finish "
                        "you will be asked to ERASE the whole source drive.")

        confirm = messagebox.askyesno(
            "Confirm",
            f"{verb} {len(files_to_process):,} file(s) "
            f"({human_size(total_size)}) to:\n  {dest_path}\n\n"
            f"Sort mode:       {sort_label.strip()}\n"
            f"Duplicate mode:  {dict(DUP_MODES)[dup]}\n"
            f"Double-check:    {dict(VERIFY_MODES)[effective_verify]}\n"
            f"{warning}\n\nProceed?"
        )
        if not confirm:
            return
        self._format_eligible = True

        self._set_controls_running(True)
        self.progress.stop()
        self.progress.configure(mode="determinate",
                                maximum=len(files_to_process), value=0)
        self.progress_label.configure(
            text=f"Starting…  0 / {len(files_to_process):,}")
        self._log_header(
            f"{verb.upper()}  •  {len(files_to_process):,} files "
            f"({human_size(total_size)})  →  {dest_path}")
        self._log_info(
            f"sort: {sort_label.strip()}  •  duplicates: {dict(DUP_MODES)[dup]}  "
            f"•  verify: {dict(VERIFY_MODES)[effective_verify]}  "
            f"•  sync writes: {'on' if self.sync_writes.get() else 'off'}")

        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._transfer_worker,
            args=(files_to_process, dest_path, op, sort_key, dup, effective_verify),
            daemon=True)
        self.worker.start()

    # ---------------- Verify worker ---------------- #
    def _verify_worker(self, files, dest_root: Path,
                       sort_key: str, verify: str) -> None:
        verbose = bool(self.verbose_log.get())
        present = missing = mismatch = errors = 0
        bytes_seen = 0
        missing_list: list[Path] = []
        mismatch_list: list[Path] = []
        total = len(files)
        t_start = time.time()
        last_report = t_start

        for i, src in enumerate(files, start=1):
            if self.cancel_event.is_set():
                self.msg_queue.put(("log", (self.LVL_WARN,
                                            f"Cancelled at {i - 1:,}/{total:,}.")))
                break
            try:
                size = safe_size(src)
                bytes_seen += size
                subfolder = build_target_subfolder(src, src.suffix.lower(), sort_key)
                expected = dest_root / subfolder / src.name
                if not expected.exists():
                    missing += 1
                    missing_list.append(src)
                    if verbose:
                        self.msg_queue.put(("log", (self.LVL_WARN,
                            f"  ✗ absent:  {src.name}  →  expected {expected}")))
                    elif missing <= 50:
                        self.msg_queue.put(("log", (self.LVL_WARN,
                                                    f"  • missing: {expected}")))
                else:
                    t0 = time.time()
                    vok, reason = verify_copy(src, expected, verify)
                    dt = time.time() - t0
                    if vok:
                        present += 1
                        if verbose:
                            self.msg_queue.put(("log", (self.LVL_DEBUG,
                                f"  ✓ present: {src.name}  ({human_size(size)}, "
                                f"checked in {human_duration(dt)})")))
                    else:
                        mismatch += 1
                        mismatch_list.append(src)
                        if mismatch <= 50:
                            self.msg_queue.put(("log", (self.LVL_ERROR,
                                f"  ✗ mismatch: {src.name} ({reason})")))
            except Exception as e:
                errors += 1
                self.msg_queue.put(("log", (self.LVL_ERROR,
                                            f"  ✗ error {src.name}: {e}")))

            now = time.time()
            if i % 10 == 0 or i == total or (now - last_report) >= 1.0:
                rate_files = i / (now - t_start) if now > t_start else 0
                rate_bytes = bytes_seen / (now - t_start) if now > t_start else 0
                self.msg_queue.put(
                    ("progress",
                     (i, total, src.name, rate_files, rate_bytes)))
                last_report = now

        elapsed = time.time() - t_start
        self.msg_queue.put(("verify_done",
                            (present, missing, mismatch, errors, total,
                             missing_list, mismatch_list, dest_root, sort_key,
                             elapsed, bytes_seen)))

    # ---------------- Transfer worker ---------------- #
    def _transfer_worker(self, files, dest_root: Path, op: str,
                         sort_key: str, dup: str, verify: str) -> None:
        verbose = bool(self.verbose_log.get())
        sync = bool(self.sync_writes.get())
        drop_cache = (verify == "hash")
        ok = errors = skipped = overwritten = renamed = erased = verify_fail = 0
        bytes_done = 0
        total = len(files)
        t_start = time.time()
        last_report = t_start

        for i, src in enumerate(files, start=1):
            if self.cancel_event.is_set():
                self.msg_queue.put(("log", (self.LVL_WARN,
                                            f"Cancelled at {i - 1:,}/{total:,}.")))
                break
            t_file = time.time()
            try:
                src_size = safe_size(src)
                subfolder = build_target_subfolder(src, src.suffix.lower(), sort_key)
                target_dir = dest_root / subfolder
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / src.name
                action_note = ""

                if target.exists() and target.resolve() == src.resolve():
                    skipped += 1
                    if verbose:
                        self.msg_queue.put(("log", (self.LVL_DEBUG,
                            f"  · skip (same path): {src.name}")))
                    self._tick_progress(i, total, src.name,
                                        bytes_done, t_start, last_report)
                    last_report = time.time()
                    continue

                if target.exists():
                    if dup == "skip":
                        skipped += 1
                        if verbose:
                            self.msg_queue.put(("log", (self.LVL_DEBUG,
                                f"  · skip (exists): {target}")))
                        self._tick_progress(i, total, src.name,
                                            bytes_done, t_start, last_report)
                        last_report = time.time()
                        continue
                    elif dup == "overwrite":
                        try:
                            if target.is_file():
                                target.unlink()
                            overwritten += 1
                            action_note = " [overwrote existing]"
                        except OSError as e:
                            self.msg_queue.put(("log", (self.LVL_ERROR,
                                f"  ✗ could not overwrite {target}: {e}")))
                            errors += 1
                            self._tick_progress(i, total, src.name,
                                                bytes_done, t_start, last_report)
                            last_report = time.time()
                            continue
                    elif dup == "erase":
                        if files_identical(src, target, verify):
                            try:
                                src.unlink()
                                erased += 1
                                if verbose:
                                    self.msg_queue.put(("log", (self.LVL_DEBUG,
                                        f"  · erased source (identical copy at "
                                        f"destination): {src.name}")))
                            except OSError as e:
                                errors += 1
                                self.msg_queue.put(("log", (self.LVL_ERROR,
                                    f"  ✗ could not erase source {src.name}: {e}")))
                            self._tick_progress(i, total, src.name,
                                                bytes_done, t_start, last_report)
                            last_report = time.time()
                            continue
                        # Same name, different content — a genuinely new
                        # file, so keep both instead of deleting anything.
                        target = unique_destination(target)
                        renamed += 1
                        action_note = f" [contents differ — renamed to {target.name}]"
                    else:  # rename
                        target = unique_destination(target)
                        renamed += 1
                        action_note = f" [renamed to {target.name}]"

                if op == "move":
                    if verify == "off":
                        shutil.move(str(src), str(target))
                        bytes_done += src_size
                        if sync:
                            sync_err = flush_to_disk(target)
                            if sync_err:
                                self.msg_queue.put(("log", (self.LVL_WARN,
                                    f"  ! moved but could not sync "
                                    f"{target.name}: {sync_err}")))
                    else:
                        # Copy first, verify, then delete source.
                        shutil.copy2(str(src), str(target))
                        bytes_done += src_size
                        if sync:
                            sync_err = flush_to_disk(target, drop_cache=drop_cache)
                            if sync_err:
                                verify_fail += 1
                                errors += 1
                                self.msg_queue.put(("log", (self.LVL_ERROR,
                                    f"  ✗ sync failed: {src.name} ({sync_err}) "
                                    f"— source kept, bad copy removed")))
                                try:
                                    target.unlink()
                                except OSError:
                                    pass
                                self._tick_progress(i, total, src.name,
                                                    bytes_done, t_start, last_report)
                                last_report = time.time()
                                continue
                        vok, reason = verify_copy(src, target, verify)
                        if not vok:
                            verify_fail += 1
                            errors += 1
                            self.msg_queue.put(("log", (self.LVL_ERROR,
                                f"  ✗ verify failed: {src.name} ({reason}) "
                                f"— source kept, bad copy removed")))
                            try:
                                target.unlink()
                            except OSError:
                                pass
                            self._tick_progress(i, total, src.name,
                                                bytes_done, t_start, last_report)
                            last_report = time.time()
                            continue
                        try:
                            src.unlink()
                        except OSError as e:
                            self.msg_queue.put(("log", (self.LVL_WARN,
                                f"  ! copied + verified, but could not remove source "
                                f"{src.name}: {e}")))
                else:
                    shutil.copy2(str(src), str(target))
                    bytes_done += src_size
                    if sync:
                        sync_err = flush_to_disk(target, drop_cache=drop_cache)
                        if sync_err:
                            verify_fail += 1
                            errors += 1
                            self.msg_queue.put(("log", (self.LVL_ERROR,
                                f"  ✗ sync failed: {src.name} ({sync_err}) "
                                f"— bad copy removed")))
                            try:
                                target.unlink()
                            except OSError:
                                pass
                            self._tick_progress(i, total, src.name,
                                                bytes_done, t_start, last_report)
                            last_report = time.time()
                            continue
                    if verify != "off":
                        vok, reason = verify_copy(src, target, verify)
                        if not vok:
                            verify_fail += 1
                            errors += 1
                            self.msg_queue.put(("log", (self.LVL_ERROR,
                                f"  ✗ verify failed: {src.name} ({reason}) "
                                f"— bad copy removed")))
                            try:
                                target.unlink()
                            except OSError:
                                pass
                            self._tick_progress(i, total, src.name,
                                                bytes_done, t_start, last_report)
                            last_report = time.time()
                            continue
                ok += 1

                if verbose:
                    dt_file = time.time() - t_file
                    rel_target = target.relative_to(dest_root)
                    verb = "moved" if op == "move" else "copied"
                    self.msg_queue.put(("log", (self.LVL_DEBUG,
                        f"  ✓ {verb} {src.name} ({human_size(src_size)})  →  "
                        f"{rel_target.parent}/  in {human_duration(dt_file)}{action_note}")))

            except Exception as e:
                errors += 1
                self.msg_queue.put(("log", (self.LVL_ERROR,
                    f"  ✗ {src.name}  →  {e}")))

            now = time.time()
            if i % 5 == 0 or i == total or (now - last_report) >= 1.0:
                rate_files = i / (now - t_start) if now > t_start else 0
                rate_bytes = bytes_done / (now - t_start) if now > t_start else 0
                self.msg_queue.put(
                    ("progress",
                     (i, total, src.name, rate_files, rate_bytes)))
                last_report = now

        elapsed = time.time() - t_start
        self.msg_queue.put(("done",
                            (ok, errors, skipped, overwritten, renamed, erased,
                             verify_fail, total, op, elapsed, bytes_done)))

    def _tick_progress(self, i, total, name, bytes_done, t_start, last_report):
        """Send a progress update to the UI thread (helper used inside continue branches)."""
        now = time.time()
        if i % 5 == 0 or i == total or (now - last_report) >= 1.0:
            rate_files = i / (now - t_start) if now > t_start else 0
            rate_bytes = bytes_done / (now - t_start) if now > t_start else 0
            self.msg_queue.put(("progress",
                                (i, total, name, rate_files, rate_bytes)))

    def _cancel_run(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self._log_warn("Cancel requested — finishing current file.")

    # ============================================================== #
    #  MESSAGE PUMP                                                  #
    # ============================================================== #
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "scan_progress":
                    self.progress_label.configure(
                        text=f"Scanning…  {payload:,} files found so far")
                elif kind == "scan_done":
                    files, counts, sizes, elapsed = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._finalize_scan(files, counts, sizes, elapsed)
                    self.progress_label.configure(
                        text=f"Scan finished in {human_duration(elapsed)}.")
                    self._set_controls_running(False)
                elif kind == "scan_error":
                    err, tb = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._log_error(f"Scan error: {err}")
                    self._log_debug(tb)
                    messagebox.showerror("Scan error", str(err))
                    self._set_controls_running(False)
                elif kind == "progress":
                    i, total, name, rate_files, rate_bytes = payload
                    self.progress.configure(value=i)
                    pct = (i / total * 100.0) if total else 0
                    eta = ""
                    if rate_files > 0 and i < total:
                        remaining = (total - i) / rate_files
                        eta = f"   ETA: {human_duration(remaining)}"
                    self.progress_label.configure(
                        text=(f"{i:,} / {total:,}  ({pct:.1f}%)   "
                              f"{rate_files:.1f} files/s   {human_rate(rate_bytes)}{eta}   "
                              f"—   {name}"))
                elif kind == "log":
                    # payload is (level, msg)
                    level, msg = payload
                    self._log(msg, level=level)
                elif kind == "done":
                    (ok, errors, skipped, overwritten, renamed, erased,
                     verify_fail, total, op, elapsed, bytes_done) = payload
                    verb = "Moved" if op == "move" else "Copied"
                    parts = [f"✓ {verb} {ok:,}/{total:,}"]
                    if renamed:      parts.append(f"renamed {renamed:,}")
                    if overwritten:  parts.append(f"overwrote {overwritten:,}")
                    if skipped:      parts.append(f"skipped {skipped:,}")
                    if erased:       parts.append(f"erased {erased:,}")
                    if verify_fail:  parts.append(f"verify failed {verify_fail:,}")
                    if errors:       parts.append(f"errors {errors:,}")
                    summary = "   •   ".join(parts)
                    rate = bytes_done / elapsed if elapsed > 0 else 0
                    fullsummary = (f"{summary}   •   {human_size(bytes_done)} "
                                   f"in {human_duration(elapsed)} "
                                   f"({human_rate(rate)})")
                    level = self.LVL_ERROR if errors else self.LVL_SUCCESS
                    self._log(fullsummary, level=level)
                    self.progress_label.configure(text=fullsummary)
                    self._set_controls_running(False)
                    self._maybe_close_file_log()
                    messagebox.showinfo("Finished", fullsummary)
                    self._maybe_format_source(errors, verify_fail)
                elif kind == "drives":
                    self._update_drives(payload)
                elif kind == "omarchy_theme":
                    self._recheck_omarchy_theme()
                elif kind == "mount_result":
                    self._handle_mount_result(payload)
                elif kind == "unmount_result":
                    self._handle_unmount_result(payload)
                elif kind == "format_result":
                    self._handle_format_result(payload)
                elif kind == "verify_done":
                    self._handle_verify_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ============================================================== #
    #  UI helpers                                                    #
    # ============================================================== #
    def _set_controls_running(self, running: bool) -> None:
        self.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.cancel_btn.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _clear_log(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    # ----- Logging primitives ----- #
    def _log(self, msg: str, level: str = "INFO") -> None:
        """Append a log line with a colored severity tag."""
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # ms precision
        prefix_lvl = level if level in ("DEBUG", "INFO", "WARN", "ERROR", "OK", "HEAD") else "INFO"
        line = f"[{stamp}] {prefix_lvl:<5}  {msg}\n"
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line, prefix_lvl)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)
        # Mirror to file if enabled.
        if self._logfile_handle is not None:
            try:
                self._logfile_handle.write(line)
                self._logfile_handle.flush()
            except Exception:
                pass

    def _log_debug(self, msg: str) -> None:
        # DEBUG only shown when verbose, but always written to file log.
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] DEBUG  {msg}\n"
        if bool(self.verbose_log.get()):
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, line, "DEBUG")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        if self._logfile_handle is not None:
            try:
                self._logfile_handle.write(line)
                self._logfile_handle.flush()
            except Exception:
                pass

    def _log_info(self, msg: str, level: str = "INFO") -> None:
        self._log(msg, level=level)

    def _log_warn(self, msg: str) -> None:
        self._log(msg, level=self.LVL_WARN)

    def _log_error(self, msg: str) -> None:
        self._log(msg, level=self.LVL_ERROR)

    def _log_success(self, msg: str) -> None:
        self._log(msg, level=self.LVL_SUCCESS)

    def _log_header(self, msg: str) -> None:
        self._log("─" * 64, level=self.LVL_HEADER)
        self._log(msg, level=self.LVL_HEADER)
        self._log("─" * 64, level=self.LVL_HEADER)

    # ----- File logging ----- #
    def _maybe_open_file_log(self, dest_path: Path, mode: str) -> None:
        if not bool(self.log_to_file.get()):
            return
        try:
            dest_path.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._logfile_path = dest_path / f"organizer_{mode}_{stamp}.log"
            self._logfile_handle = open(self._logfile_path, "a", encoding="utf-8")
            self._logfile_handle.write(
                f"\n=== File Organizer session log — "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            self._logfile_handle.write(
                f"Source:      {self.source_path.get()}\n"
                f"Destination: {dest_path}\n"
                f"Mode:        {mode}\n"
                f"Operation:   {self.operation.get()}\n"
                f"Sort:        {self.sort_mode_label.get()}\n"
                f"Duplicate:   {self.dup_mode.get()}\n"
                f"Verify:      {self.verify_mode.get()}\n"
                f"Verbose:     {self.verbose_log.get()}\n"
                f"=================================================\n")
            self._logfile_handle.flush()
            self._log_info(f"Log file opened: {self._logfile_path}")
        except OSError as e:
            self._logfile_path = None
            self._logfile_handle = None
            self._log_warn(f"Could not open log file: {e}")

    def _maybe_close_file_log(self) -> None:
        if self._logfile_handle is not None:
            try:
                self._logfile_handle.write(
                    f"=== Session ended {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                self._logfile_handle.close()
                self._log_info(f"Log file closed: {self._logfile_path}")
            except Exception:
                pass
            self._logfile_handle = None
            self._logfile_path = None

    # ----- Verify completion handling ----- #
    def _handle_verify_done(self, payload) -> None:
        (present, missing, mismatch, errors, total,
         missing_list, mismatch_list, dest_root, sort_key,
         elapsed, bytes_seen) = payload
        parts = [f"✓ Verified {present:,}/{total:,}"]
        if missing:  parts.append(f"missing {missing:,}")
        if mismatch: parts.append(f"mismatch {mismatch:,}")
        if errors:   parts.append(f"errors {errors:,}")
        summary = "   •   ".join(parts)
        rate = bytes_seen / elapsed if elapsed > 0 else 0
        fullsummary = (f"{summary}   •   {human_size(bytes_seen)} "
                       f"in {human_duration(elapsed)} "
                       f"({human_rate(rate)})")
        level = self.LVL_ERROR if (missing or mismatch or errors) else self.LVL_SUCCESS
        self._log(fullsummary, level=level)
        self.progress_label.configure(text=fullsummary)
        self._set_controls_running(False)
        self._maybe_close_file_log()

        needs_transfer = missing_list + mismatch_list
        if not needs_transfer:
            messagebox.showinfo("Verify complete",
                                f"{fullsummary}\n\nAll source files are present at the destination.")
            return

        sample = needs_transfer[:5]
        sample_txt = "\n".join(f"  • {p.name}" for p in sample)
        more = f"\n  … and {len(needs_transfer) - len(sample):,} more" \
               if len(needs_transfer) > len(sample) else ""
        ans = messagebox.askyesno(
            "Verify complete — copy missing files?",
            f"{fullsummary}\n\n"
            f"{len(needs_transfer):,} file(s) need to be transferred:\n"
            f"{sample_txt}{more}\n\n"
            f"Copy these now to the destination? (Sources will be kept — "
            f"this is always a copy, never a move.)"
        )
        if not ans:
            return
        self._run_fixup_transfer(needs_transfer, dest_root, sort_key)

    def _run_fixup_transfer(self, files, dest_root: Path, sort_key: str) -> None:
        """Kick off a copy of just the missing/mismatched files."""
        if self.worker and self.worker.is_alive():
            return
        dup = "overwrite"
        verify = self.verify_mode.get() or "quick"
        self._format_eligible = False  # never offer a format after a fix-up
        self._set_controls_running(True)
        self.progress.stop()
        self.progress.configure(mode="determinate",
                                maximum=len(files), value=0)
        self.progress_label.configure(
            text=f"Fixing…  0 / {len(files):,}")
        self._log_header(
            f"FIX-UP COPY  •  {len(files):,} missing/mismatched file(s)  →  {dest_root}")
        self._log_info(f"sort: {SORT_LABEL_BY_KEY.get(sort_key, sort_key)}  •  "
                       f"duplicates: overwrite  •  verify: {verify}  •  always copy")
        self._maybe_open_file_log(dest_root, "fixup")
        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._transfer_worker,
            args=(files, dest_root, "copy", sort_key, dup, verify),
            daemon=True)
        self.worker.start()

    # ============================================================== #
    #  CLOSE                                                         #
    # ============================================================== #
    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            ans = messagebox.askyesno("Quit?",
                                      "An operation is still running. Cancel and quit?")
            if not ans:
                return
            self.cancel_event.set()
        # Persist last-used profile name
        try:
            self._record_locations()
            save_profiles_to_disk(self.profiles, self.current_profile.get(),
                                  self.last_locations)
        except Exception:
            pass
        self._maybe_close_file_log()
        self.root.destroy()


# =========================================================================== #
#  Entry point                                                                 #
# =========================================================================== #
def main() -> None:
    palette = load_omarchy_palette()
    if palette is not None:
        THEMES["dark"] = palette      # "dark" *is* the Omarchy theme here
    root = tk.Tk(className="FileOrganizer")    # WM_CLASS for the .desktop entry
    resolve_fonts()          # swap Windows fonts for installed ones before any widgets
    FileOrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
