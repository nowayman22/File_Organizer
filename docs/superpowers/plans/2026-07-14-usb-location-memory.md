# USB-Aware Locations & Linux Optimizations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Photo Organizer remember source/destination locations across sessions (device-aware, keyed by USB drive label), add a one-click USB drive picker, and make USB transfers physically safe (fsync + cache-bypassing hash verify).

**Architecture:** Everything stays in the single `Organizer_Linux.py` file, following its existing layout: pure helper functions at module level (testable), UI/threading inside `PhotoOrganizerApp`. Drive detection shells out to `lsblk -J` (polled every 3 s via the existing `root.after` + `msg_queue` pattern) with a `/run/media` scan fallback. Location memory extends the existing `~/.photo_organizer/profiles.json`.

**Tech Stack:** Python 3.9+ stdlib only (tkinter, subprocess, json, os, shutil, hashlib, threading). Tests: stdlib `unittest` in a new `tests/` package.

**Spec:** `docs/superpowers/specs/2026-07-14-usb-location-memory-design.md`

## Global Constraints

- Stdlib only — no new dependencies (Pillow stays optional, as today).
- Single app file: all app code goes in `Organizer_Linux.py`; only tests live elsewhere.
- **No git repo exists in this project (user's choice)** — commit steps are replaced by a verification step: run the full test suite and `python -m py_compile Organizer_Linux.py`.
- All new OS interactions must degrade gracefully: `lsblk` missing → fallback scan; `posix_fadvise` missing → skip; bad JSON in profiles → ignore. New helpers must never raise into the UI thread.
- Run all commands from the project root: `/home/nikits/Projects/Organizer`.
- Tests run headless-safe: they import `Organizer_Linux` (safe — no `tk.Tk()` at import time) but must never construct Tk objects.
- New checkbox copy (exact): `Sync writes to device (recommended for USB)`. Combobox placeholder (exact): `no USB drives detected`. Hint copy (exact): `Last used drive 'LABEL' not connected.` and `✓ LABEL reconnected`.

---

### Task 1: `UsbDrive` record + `parse_lsblk_drives()` (pure lsblk-JSON parsing)

**Files:**
- Modify: `Organizer_Linux.py` (imports near line 60-74; new section after the `Profiles` section, ~line 380)
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_organizer.py`

**Interfaces:**
- Produces: `class UsbDrive(NamedTuple)` with fields `label: str`, `mountpoint: str`, `size: int`, `free: int`, `device: str`.
- Produces: `parse_lsblk_drives(data: dict) -> list[UsbDrive]` — takes parsed `lsblk -J -b` JSON, returns mounted USB/removable partitions, excluding system mounts.
- Later tasks rely on these exact names and field names.

- [ ] **Step 1: Create the test package and write the failing tests**

Create empty `tests/__init__.py`, then `tests/test_organizer.py`:

```python
"""Unit tests for the pure helper functions in Organizer_Linux.py.

Run from the project root:  python -m unittest tests.test_organizer -v
These tests never create Tk widgets, so they run headless.
"""
import unittest

import Organizer_Linux as org


LSBLK_FIXTURE = {
    "blockdevices": [
        {   # internal NVMe disk — must be excluded (system mounts, tran=nvme)
            "name": "nvme0n1", "label": None, "mountpoint": None,
            "size": 512110190592, "fsavail": None, "rm": False,
            "tran": "nvme", "type": "disk",
            "children": [
                {"name": "nvme0n1p1", "label": None, "mountpoint": "/boot",
                 "size": 536870912, "fsavail": "200000000", "rm": False,
                 "tran": None, "type": "part"},
                {"name": "nvme0n1p2", "label": None, "mountpoint": "/",
                 "size": 511560000000, "fsavail": "100000000000", "rm": False,
                 "tran": None, "type": "part"},
            ]},
        {   # USB stick: tran=usb on the disk, child partition mounted
            "name": "sda", "label": None, "mountpoint": None,
            "size": 62109253632, "fsavail": None, "rm": True,
            "tran": "usb", "type": "disk",
            "children": [
                {"name": "sda1", "label": "SANDISK",
                 "mountpoint": "/run/media/nikits/SANDISK",
                 "size": 62108205056, "fsavail": "12100000000", "rm": True,
                 "tran": None, "type": "part"},
            ]},
        {   # SD card reader: tran is None but rm is true (older lsblk: "1")
            "name": "mmcblk0", "label": None, "mountpoint": None,
            "size": 31914983424, "fsavail": None, "rm": "1",
            "tran": None, "type": "disk",
            "children": [
                {"name": "mmcblk0p1", "label": None,
                 "mountpoint": "/run/media/nikits/9016-4EF8",
                 "size": 31913934848, "fsavail": "28000000000", "rm": "1",
                 "tran": None, "type": "part"},
            ]},
        {   # USB disk with unmounted partition — must be excluded
            "name": "sdb", "label": None, "mountpoint": None,
            "size": 1000204886016, "fsavail": None, "rm": False,
            "tran": "usb", "type": "disk",
            "children": [
                {"name": "sdb1", "label": "ARCHIVE", "mountpoint": None,
                 "size": 1000203837440, "fsavail": None, "rm": False,
                 "tran": None, "type": "part"},
            ]},
    ]
}


class ParseLsblkTests(unittest.TestCase):
    def test_finds_mounted_usb_partition_with_inherited_transport(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        sandisk = [d for d in drives if d.label == "SANDISK"]
        self.assertEqual(len(sandisk), 1)
        d = sandisk[0]
        self.assertEqual(d.mountpoint, "/run/media/nikits/SANDISK")
        self.assertEqual(d.size, 62108205056)
        self.assertEqual(d.free, 12100000000)
        self.assertEqual(d.device, "sda1")

    def test_removable_flag_string_counts_as_removable(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        sd = [d for d in drives if d.device == "mmcblk0p1"]
        self.assertEqual(len(sd), 1)
        # No filesystem label: falls back to the mountpoint's last component.
        self.assertEqual(sd[0].label, "9016-4EF8")

    def test_excludes_system_mounts_and_unmounted_partitions(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        mountpoints = {d.mountpoint for d in drives}
        self.assertNotIn("/", mountpoints)
        self.assertNotIn("/boot", mountpoints)
        self.assertEqual(len(drives), 2)  # SANDISK + SD card only

    def test_empty_or_malformed_input_returns_empty_list(self):
        self.assertEqual(org.parse_lsblk_drives({}), [])
        self.assertEqual(org.parse_lsblk_drives({"blockdevices": None}), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_organizer -v`
Expected: 4 errors, each `AttributeError: module 'Organizer_Linux' has no attribute 'parse_lsblk_drives'` (the `UsbDrive` import failure surfaces the same way).

- [ ] **Step 3: Implement `UsbDrive` and `parse_lsblk_drives`**

In `Organizer_Linux.py`, extend the typing import (line 74):

```python
from typing import NamedTuple, Optional
```

Add `import subprocess` to the stdlib import block (alphabetical, after `import struct`) — used in Task 2, harmless to add now with the section it belongs to.

Add a new section after `save_profiles_to_disk` (before the `Helpers` section, ~line 381):

```python
# =========================================================================== #
#  Removable-drive detection                                                   #
# =========================================================================== #
class UsbDrive(NamedTuple):
    label: str
    mountpoint: str
    size: int        # bytes
    free: int        # bytes
    device: str      # kernel name, e.g. "sda1" ("" for fallback scan)


# Mountpoints that are never a photo source/destination, even on a
# removable-flagged device (some SATA/eSATA disks report rm=1).
_SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/var", "[SWAP]"}


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
    """Extract mounted USB/removable partitions from parsed `lsblk -J -b`.

    The usb/removable property usually sits on the parent disk while the
    mountpoint sits on the child partition, so it is inherited downward.
    """
    drives: list[UsbDrive] = []

    def walk(node, inherited: bool) -> None:
        if not isinstance(node, dict):
            return
        is_usb = (inherited or node.get("tran") == "usb"
                  or _coerce_bool(node.get("rm")))
        mp = node.get("mountpoint")
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 4 tests passed.

- [ ] **Step 5: Verify the app still compiles**

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

---

### Task 2: `list_usb_drives()` with `/run/media` fallback

**Files:**
- Modify: `Organizer_Linux.py` (append to the Removable-drive detection section from Task 1)
- Test: `tests/test_organizer.py`

**Interfaces:**
- Consumes: `UsbDrive`, `parse_lsblk_drives` (Task 1).
- Produces: `list_usb_drives() -> list[UsbDrive]` — never raises; `lsblk` first, `_scan_media_dirs()` on any failure.
- Produces: `_scan_media_dirs() -> list[UsbDrive]` — lists mounted dirs under `/run/media/<user>` and `/media/<user>` with `statvfs` sizes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
import os
import tempfile
from pathlib import Path
from unittest import mock


class ScanMediaDirsTests(unittest.TestCase):
    def test_scans_media_roots_with_statvfs_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SANDISK").mkdir()
            (Path(tmp) / "BACKUP").mkdir()
            (Path(tmp) / "notadir.txt").write_text("x")
            with mock.patch.object(org, "_media_roots",
                                   return_value=[Path(tmp)]):
                drives = org._scan_media_dirs()
        labels = sorted(d.label for d in drives)
        self.assertEqual(labels, ["BACKUP", "SANDISK"])
        for d in drives:
            self.assertGreater(d.size, 0)
            self.assertGreaterEqual(d.free, 0)
            self.assertEqual(d.device, "")

    def test_missing_roots_give_empty_list(self):
        with mock.patch.object(org, "_media_roots",
                               return_value=[Path("/nonexistent-xyz")]):
            self.assertEqual(org._scan_media_dirs(), [])


class ListUsbDrivesTests(unittest.TestCase):
    def test_falls_back_to_scan_when_lsblk_missing(self):
        sentinel = [org.UsbDrive("X", "/run/media/u/X", 1, 1, "")]
        with mock.patch.object(org.subprocess, "run",
                               side_effect=FileNotFoundError("no lsblk")), \
             mock.patch.object(org, "_scan_media_dirs",
                               return_value=sentinel):
            self.assertEqual(org.list_usb_drives(), sentinel)

    def test_uses_lsblk_output_when_available(self):
        import json as _json
        proc = mock.Mock(returncode=0, stdout=_json.dumps(LSBLK_FIXTURE))
        with mock.patch.object(org.subprocess, "run", return_value=proc):
            drives = org.list_usb_drives()
        self.assertEqual({d.label for d in drives}, {"SANDISK", "9016-4EF8"})

    def test_never_raises_on_garbage_output(self):
        proc = mock.Mock(returncode=0, stdout="not json")
        with mock.patch.object(org.subprocess, "run", return_value=proc), \
             mock.patch.object(org, "_scan_media_dirs", return_value=[]):
            self.assertEqual(org.list_usb_drives(), [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_organizer -v`
Expected: Task 1 tests still pass; the 5 new tests error with `AttributeError` (`_media_roots` / `_scan_media_dirs` / `list_usb_drives` not defined).

- [ ] **Step 3: Implement the functions**

Append to the Removable-drive detection section in `Organizer_Linux.py`:

```python
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
    """All currently mounted USB/removable drives. Never raises."""
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,LABEL,MOUNTPOINT,SIZE,FSAVAIL,RM,TRAN,TYPE"],
            capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            return parse_lsblk_drives(json.loads(proc.stdout))
    except (OSError, subprocess.SubprocessError,
            json.JSONDecodeError, ValueError):
        pass
    return _scan_media_dirs()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 9 tests passed.

- [ ] **Step 5: Real-machine smoke check**

Run: `python -c "import Organizer_Linux as o; [print(d) for d in o.list_usb_drives()]"`
Expected: one line per currently plugged-in USB drive (or nothing if none is connected); no traceback either way.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

---

### Task 3: Location records — `make_location_record()` / `resolve_location()`

**Files:**
- Modify: `Organizer_Linux.py` (append to the Removable-drive detection section)
- Test: `tests/test_organizer.py`

**Interfaces:**
- Consumes: `UsbDrive` (Task 1).
- Produces: `make_location_record(path_str: str, drives: list[UsbDrive]) -> dict` — always includes `"path"`; adds `"label"` and `"rel"` when the path is under a drive's mountpoint (`rel` is `"."` for the mountpoint itself).
- Produces: `resolve_location(record, drives) -> tuple[Optional[str], Optional[str]]` — returns `(path_to_use, None)` when resolvable, `(None, missing_label)` when the remembered drive isn't connected, `(None, None)` when there's nothing to restore.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
class LocationRecordTests(unittest.TestCase):
    def _drives(self, tmp):
        return [org.UsbDrive("SANDISK", str(Path(tmp) / "SANDISK"),
                             64_000_000_000, 12_000_000_000, "sda1")]

    def test_record_on_drive_stores_label_and_rel(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "SANDISK"
            (mount / "DCIM").mkdir(parents=True)
            rec = org.make_location_record(str(mount / "DCIM"),
                                           self._drives(tmp))
        self.assertEqual(rec["label"], "SANDISK")
        self.assertEqual(rec["rel"], "DCIM")
        self.assertEqual(rec["path"], str(mount / "DCIM"))

    def test_record_at_mountpoint_root_uses_dot_rel(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "SANDISK"
            mount.mkdir()
            rec = org.make_location_record(str(mount), self._drives(tmp))
        self.assertEqual(rec["rel"], ".")

    def test_record_off_drive_stores_path_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = org.make_location_record("/home/nikits/Pictures",
                                           self._drives(tmp))
        self.assertEqual(rec, {"path": "/home/nikits/Pictures"})

    def test_resolve_existing_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, missing = org.resolve_location({"path": tmp}, [])
        self.assertEqual(path, tmp)
        self.assertIsNone(missing)

    def test_resolve_by_label_when_path_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "SANDISK"
            (mount / "DCIM").mkdir(parents=True)
            rec = {"path": "/run/media/old/SANDISK/DCIM",
                   "label": "SANDISK", "rel": "DCIM"}
            path, missing = org.resolve_location(rec, self._drives(tmp))
        self.assertEqual(path, str(mount / "DCIM"))
        self.assertIsNone(missing)

    def test_resolve_falls_back_to_mountpoint_when_rel_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "SANDISK"
            mount.mkdir()
            rec = {"path": "/gone", "label": "SANDISK", "rel": "DCIM"}
            path, missing = org.resolve_location(rec, self._drives(tmp))
        self.assertEqual(path, str(mount))
        self.assertIsNone(missing)

    def test_resolve_reports_missing_drive(self):
        rec = {"path": "/gone", "label": "SANDISK", "rel": "."}
        path, missing = org.resolve_location(rec, [])
        self.assertIsNone(path)
        self.assertEqual(missing, "SANDISK")

    def test_resolve_tolerates_junk(self):
        self.assertEqual(org.resolve_location(None, []), (None, None))
        self.assertEqual(org.resolve_location("bogus", []), (None, None))
        self.assertEqual(org.resolve_location({}, []), (None, None))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_organizer -v`
Expected: earlier tests pass; the 8 new ones error with `AttributeError: ... 'make_location_record'` / `'resolve_location'`.

- [ ] **Step 3: Implement the functions**

Append to the Removable-drive detection section:

```python
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
        try:
            p.relative_to(Path(d.mountpoint))
        except ValueError:
            continue
        if best is None or len(d.mountpoint) > len(best.mountpoint):
            best = d      # deepest matching mountpoint wins
    if best is not None:
        record["label"] = best.label
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
        if d.label == label:
            rel = record.get("rel")
            if isinstance(rel, str) and rel not in ("", "."):
                candidate = Path(d.mountpoint) / rel
                if candidate.is_dir():
                    return str(candidate), None
            return d.mountpoint, None
    return None, label
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 17 tests passed.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

---

### Task 4: Persist `last_locations` in profiles.json

**Files:**
- Modify: `Organizer_Linux.py`:
  - `load_profiles_from_disk` (~line 342) and `save_profiles_to_disk` (~line 364)
  - Call sites: `__init__` (~line 946), `_save_current_profile` (~line 1309), `_save_profile_as` (~line 1337), `_delete_current_profile` (~line 1358), `_on_close` (~line 2146)
- Test: `tests/test_organizer.py`

**Interfaces:**
- Consumes: nothing new (pure JSON shape change).
- Produces: `load_profiles_from_disk() -> tuple[dict[str, dict], Optional[str], dict]` (third element: `last_locations`, `{}` when absent/bad).
- Produces: `save_profiles_to_disk(profiles, last_used, last_locations) -> Optional[str]` (third parameter required; callers pass `self.last_locations`).
- Produces: app attribute `self.last_locations: dict` (keys `"source"`/`"dest"`, values are location records from Task 3). Tasks 5–7 read/write this attribute.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
class PersistenceTests(unittest.TestCase):
    def _patch_paths(self, tmp):
        pdir = Path(tmp) / ".photo_organizer"
        pfile = pdir / "profiles.json"
        return mock.patch.multiple(org, PROFILES_DIR=pdir, PROFILES_FILE=pfile)

    def test_round_trips_last_locations(self):
        locations = {"source": {"path": "/x", "label": "A", "rel": "."},
                     "dest": {"path": "/y"}}
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_paths(tmp):
                err = org.save_profiles_to_disk(
                    dict(org.DEFAULT_PROFILES), "whatever", locations)
                self.assertIsNone(err)
                profiles, last_used, loaded = org.load_profiles_from_disk()
        self.assertEqual(last_used, "whatever")
        self.assertEqual(loaded, locations)
        self.assertIn("Default — Copy by Year/Month", profiles)

    def test_missing_or_bad_last_locations_gives_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_paths(tmp):
                org.PROFILES_DIR.mkdir(parents=True)
                org.PROFILES_FILE.write_text(
                    '{"profiles": {}, "last_used": null,'
                    ' "last_locations": [1, 2]}')
                _, _, loaded = org.load_profiles_from_disk()
        self.assertEqual(loaded, {})

    def test_filters_unknown_keys_and_non_dict_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_paths(tmp):
                org.PROFILES_DIR.mkdir(parents=True)
                org.PROFILES_FILE.write_text(
                    '{"profiles": {}, "last_used": null, "last_locations":'
                    ' {"source": {"path": "/x"}, "bogus": {"path": "/y"},'
                    '  "dest": "not-a-dict"}}')
                _, _, loaded = org.load_profiles_from_disk()
        self.assertEqual(loaded, {"source": {"path": "/x"}})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_organizer -v`
Expected: the 3 new tests fail — `save_profiles_to_disk` rejects the third positional argument (`TypeError`) and `load_profiles_from_disk` returns a 2-tuple (`ValueError: not enough values to unpack`).

- [ ] **Step 3: Change the two functions**

Replace `load_profiles_from_disk` (currently returns a 2-tuple):

```python
def load_profiles_from_disk() -> tuple[dict[str, dict], Optional[str], dict]:
    """Return (profiles, last_used_name, last_locations).

    Falls back to defaults / empty on any error.
    """
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
```

In `save_profiles_to_disk`, change the signature and payload line:

```python
def save_profiles_to_disk(profiles: dict[str, dict], last_used: Optional[str],
                          last_locations: dict) -> Optional[str]:
```

```python
        payload = {"profiles": to_save, "last_used": last_used,
                   "last_locations": last_locations or {}}
```

(Docstring and the rest of the body stay as-is.)

- [ ] **Step 4: Update the call sites**

In `__init__` (~line 946):

```python
        self.profiles, last_used, self.last_locations = load_profiles_from_disk()
```

The four `save_profiles_to_disk(self.profiles, <name>)` calls in `_save_current_profile`, `_save_profile_as`, `_delete_current_profile`, and `_on_close` each gain `self.last_locations` as the third argument, e.g.:

```python
        err = save_profiles_to_disk(self.profiles, name, self.last_locations)
```

and in `_on_close`:

```python
            save_profiles_to_disk(self.profiles, self.current_profile.get(),
                                  self.last_locations)
```

- [ ] **Step 5: Run the tests and compile check**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 20 tests passed.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

Run: `grep -n "save_profiles_to_disk(" Organizer_Linux.py`
Expected: every call site (there are 4 at this point, plus the def; Task 8 adds a fifth) passes three arguments.

---

### Task 5: `flush_to_disk()` + `safe_size()` + 4 MiB hash chunks

**Files:**
- Modify: `Organizer_Linux.py`:
  - `sha256_of` (~line 565): chunk default 1 MiB → 4 MiB
  - New helpers next to `sha256_of` in the Helpers section
  - `_verify_worker` (~line 1692): replace `src.stat().st_size if src.exists() else 0`
  - `_start_run` (~line 1587): replace the `total_size` generator
- Test: `tests/test_organizer.py`

**Interfaces:**
- Produces: `flush_to_disk(path: Path, drop_cache: bool = False) -> Optional[str]` — fsync; optional page-cache drop; returns error string or `None`. Task 6 calls this from `_transfer_worker`.
- Produces: `safe_size(p: Path) -> int` — `st_size` or 0, never raises.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
class FlushAndSizeTests(unittest.TestCase):
    def test_flush_ok_on_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            f.write_bytes(b"x" * 1024)
            self.assertIsNone(org.flush_to_disk(f))
            self.assertIsNone(org.flush_to_disk(f, drop_cache=True))

    def test_flush_missing_file_returns_error_string(self):
        err = org.flush_to_disk(Path("/nonexistent-xyz/a.bin"))
        self.assertIsInstance(err, str)
        self.assertTrue(err)

    def test_safe_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            f.write_bytes(b"x" * 3)
            self.assertEqual(org.safe_size(f), 3)
            self.assertEqual(org.safe_size(Path("/nonexistent-xyz")), 0)

    def test_sha256_default_chunk_is_4mib(self):
        import inspect
        sig = inspect.signature(org.sha256_of)
        self.assertEqual(sig.parameters["chunk"].default, 4 * 1024 * 1024)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_organizer -v`
Expected: 3 `AttributeError` failures (`flush_to_disk`, `safe_size`) and 1 assertion failure (chunk default is 1048576).

- [ ] **Step 3: Implement**

Change the `sha256_of` signature (~line 565):

```python
def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
```

Add below `sha256_of`:

```python
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
```

- [ ] **Step 4: Remove the duplicated stat calls**

In `_verify_worker` (~line 1692), replace:

```python
                size = src.stat().st_size if src.exists() else 0
```

with:

```python
                size = safe_size(src)
```

In `_start_run` (~line 1587), replace:

```python
        total_size = sum((p.stat().st_size for p in files_to_process
                          if p.exists()), 0)
```

with:

```python
        total_size = sum(safe_size(p) for p in files_to_process)
```

In `_transfer_worker` (~line 1755), replace:

```python
                try:
                    src_size = src.stat().st_size
                except OSError:
                    src_size = 0
```

with:

```python
                src_size = safe_size(src)
```

- [ ] **Step 5: Run the tests and compile check**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 24 tests passed.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

---

### Task 6: Sync-writes toggle (profile field, checkbox, worker integration)

**Files:**
- Modify: `Organizer_Linux.py`:
  - `PROFILE_FIELDS` (~line 280) and `DEFAULT_PROFILES` (~line 288)
  - `__init__` state vars (~line 918)
  - `_build_ui` logging row (~line 1152)
  - `_capture_profile` (~line 1230) and `_apply_profile` (~line 1249)
  - `_start_run` settings log line (~line 1663) and `_transfer_worker` (~line 1739)
- Test: manual + compile (Tk-dependent code; the underlying `flush_to_disk` is already unit-tested in Task 5)

**Interfaces:**
- Consumes: `flush_to_disk` (Task 5).
- Produces: `self.sync_writes: tk.BooleanVar` (default `True`); profile key `"sync_writes"`. No later task depends on this beyond the profile round-trip.

- [ ] **Step 1: Add the profile field and defaults**

In `PROFILE_FIELDS` add `"sync_writes"` after `"include_subdirs"`:

```python
PROFILE_FIELDS = (
    "source_path", "dest_path",
    "operation", "run_mode", "sort_mode", "dup_mode", "verify_mode",
    "include_subdirs", "sync_writes", "verbose_log", "log_to_file", "theme",
)
```

In every one of the five `DEFAULT_PROFILES` entries, add the line (after `"include_subdirs": True,`):

```python
        "sync_writes": True,
```

- [ ] **Step 2: Add the state var, checkbox, and profile plumbing**

In `__init__`, after `self.include_subdirs = ...`:

```python
        self.sync_writes     = tk.BooleanVar(value=True)
```

In `_build_ui`, in the logging row (`log_row`, ~line 1156), after the "Save log to file in destination" checkbutton:

```python
        ttk.Checkbutton(log_row, text="Sync writes to device (recommended for USB)",
                        variable=self.sync_writes).pack(side=tk.LEFT, padx=(16, 0))
```

In `_capture_profile`, after the `"include_subdirs"` line:

```python
            "sync_writes":     bool(self.sync_writes.get()),
```

In `_apply_profile`, after the `include_subdirs` line:

```python
            if "sync_writes"     in data: self.sync_writes.set(bool(data["sync_writes"]))
```

- [ ] **Step 3: Integrate into `_transfer_worker`**

At the top of `_transfer_worker`, after `verbose = bool(self.verbose_log.get())`:

```python
        sync = bool(self.sync_writes.get())
        drop_cache = (verify == "hash")
```

**Copy branch** — the `else:` of `if op == "move":` currently starts:

```python
                else:
                    shutil.copy2(str(src), str(target))
                    bytes_done += src_size
                    if verify != "off":
```

Insert the sync block between `bytes_done += src_size` and `if verify != "off":`:

```python
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
```

**Move + verify branch** — currently:

```python
                    else:
                        # Copy first, verify, then delete source.
                        shutil.copy2(str(src), str(target))
                        bytes_done += src_size
                        vok, reason = verify_copy(src, target, verify)
```

Insert the same sync block between `bytes_done += src_size` and `vok, reason = ...`, with the message noting the source is kept:

```python
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
```

**Move fast path** (verify off) — currently:

```python
                if op == "move":
                    if verify == "off":
                        shutil.move(str(src), str(target))
                        bytes_done += src_size
```

Append after `bytes_done += src_size` (file already moved — never delete on sync failure, just warn):

```python
                        if sync:
                            sync_err = flush_to_disk(target)
                            if sync_err:
                                self.msg_queue.put(("log", (self.LVL_WARN,
                                    f"  ! moved but could not sync "
                                    f"{target.name}: {sync_err}")))
```

- [ ] **Step 4: Log the sync state at run start**

In `_start_run`, extend the transfer-branch settings line (~line 1663):

```python
        self._log_info(
            f"sort: {sort_label.strip()}  •  duplicates: {dict(DUP_MODES)[dup]}  "
            f"•  verify: {dict(VERIFY_MODES)[effective_verify]}  "
            f"•  sync writes: {'on' if self.sync_writes.get() else 'off'}")
```

- [ ] **Step 5: Verify**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 24 tests passed (no regressions).

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

Functional check (writes only inside /tmp):

```bash
python - <<'EOF'
import tempfile, shutil, os
from pathlib import Path
import Organizer_Linux as org
src = Path(tempfile.mkdtemp()) / "a.jpg"; src.write_bytes(b"j" * 4096)
dst = Path(tempfile.mkdtemp()) / "a.jpg"
shutil.copy2(src, dst)
assert org.flush_to_disk(dst, drop_cache=True) is None
print("sync path OK")
EOF
```

Expected: `sync path OK`

---

### Task 7: USB quick-pick UI + 3-second polling

**Files:**
- Modify: `Organizer_Linux.py`:
  - `__init__` (state + poll kickoff, ~line 929 and ~line 970)
  - `_build_ui` Source card (~line 1024-1038) and Destination card (~line 1041-1055)
  - `_poll_queue` (~line 1898): new `"drives"` message
  - New methods after `_browse_dest` (~line 1380)
- Test: manual (Tk-dependent); parsing/listing already unit-tested

**Interfaces:**
- Consumes: `list_usb_drives`, `UsbDrive`, `human_size`.
- Produces: `self.usb_drives: list[UsbDrive]`; combos `self.src_drive_combo` / `self.dst_drive_combo`; hint labels `self.src_drive_hint` / `self.dst_drive_hint`; methods `_refresh_drives()`, `_poll_drives()`, `_update_drives(drives)`, `_pick_drive(kind)`. Task 8's reconnect logic hooks into `_update_drives` via `_try_pending_restore()` — Task 7 ships `_update_drives` already calling `self._try_pending_restore()` guarded by `hasattr`, so the tasks stay independently landable.

- [ ] **Step 1: Add state and start polling**

In `__init__`, after `self._auto_detected_key: Optional[str] = None`:

```python
        self.usb_drives: list[UsbDrive] = []
        self._pending_restore: dict[str, dict] = {}
```

At the end of `__init__`, just before `self.root.after(100, self._poll_queue)`:

```python
        self._refresh_drives()
        self.root.after(3000, self._poll_drives)
```

- [ ] **Step 2: Add the combobox rows to both cards**

In `_build_ui`, in the Source card, insert between `src_row` and `src_row2`:

```python
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
        self.src_drive_hint = ttk.Label(src, text="", style="Muted.TLabel")
        self.src_drive_hint.pack(anchor="w", pady=(4, 0))
```

In the Destination card, insert the mirrored block between `dst_row` and `dst_row2`:

```python
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
        self.dst_drive_hint = ttk.Label(dst, text="", style="Muted.TLabel")
        self.dst_drive_hint.pack(anchor="w", pady=(4, 0))
```

- [ ] **Step 3: Add the drive methods**

After `_browse_dest` (~line 1380):

```python
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
        return f"{d.label} — {human_size(d.size)} ({human_size(d.free)} free)"

    def _update_drives(self, drives: list[UsbDrive]) -> None:
        key = [(d.label, d.mountpoint, d.free) for d in drives]
        old_key = [(d.label, d.mountpoint, d.free) for d in self.usb_drives]
        self.usb_drives = drives
        if key == old_key:
            return
        values = [self._drive_display(d) for d in drives]
        for combo in (self.src_drive_combo, self.dst_drive_combo):
            current = combo.get()
            combo.configure(values=values)
            if values:
                combo.configure(state="readonly")
                if current not in values:
                    combo.set("")
            else:
                combo.set("no USB drives detected")
                combo.configure(state="disabled")
        if hasattr(self, "_try_pending_restore"):
            self._try_pending_restore()

    def _pick_drive(self, kind: str) -> None:
        combo = self.src_drive_combo if kind == "source" else self.dst_drive_combo
        idx = combo.current()
        if idx < 0 or idx >= len(self.usb_drives):
            return
        drive = self.usb_drives[idx]
        var = self.source_path if kind == "source" else self.dest_path
        hint = self.src_drive_hint if kind == "source" else self.dst_drive_hint
        var.set(drive.mountpoint)
        hint.configure(text="")
        self._log_debug(f"{kind} set from USB picker: {drive.mountpoint}")
```

- [ ] **Step 4: Handle the `"drives"` message in `_poll_queue`**

In `_poll_queue`, add before the `elif kind == "verify_done":` line:

```python
                elif kind == "drives":
                    self._update_drives(payload)
```

- [ ] **Step 5: Verify (compile + manual)**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 24 tests passed.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

Manual (needs the desktop session): `python Organizer_Linux.py` —
- Both cards show a `USB:` row; with no drive plugged it reads `no USB drives detected` and is greyed out.
- Plug a USB stick: within ~3 s the combobox enables and lists `LABEL — size (free free)`.
- Selecting the entry fills the path field with the mountpoint; ⟳ forces an immediate refresh.
- Unplug: within ~3 s the entry disappears.

---

### Task 8: Startup restore, run/close recording, reconnect hint

**Files:**
- Modify: `Organizer_Linux.py`:
  - `__init__` (~line 957-963, around the profile-apply block)
  - `_start_run` (~line 1585, after the file-type checks)
  - `_on_close` (~line 2144)
  - New methods next to the Task 7 drive methods
- Test: manual end-to-end (resolve/record logic already unit-tested in Task 3)

**Interfaces:**
- Consumes: `resolve_location`, `make_location_record` (Task 3), `self.last_locations` (Task 4), `self.usb_drives` / hint labels / `_update_drives` (Task 7).
- Produces: `_restore_locations()` (called once at startup), `_record_locations()` (called at run start and on close), `_try_pending_restore()` (called by `_update_drives`).

- [ ] **Step 1: Add the three methods**

After `_pick_drive`:

```python
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

    def _record_locations(self) -> None:
        """Snapshot current paths into self.last_locations (not yet saved)."""
        for kind, var in (("source", self.source_path),
                          ("dest", self.dest_path)):
            path = var.get().strip()
            if path:
                self.last_locations[kind] = make_location_record(
                    path, self.usb_drives)
```

- [ ] **Step 2: Wire startup restore into `__init__`**

The Task 7 kickoff at the end of `__init__` becomes (synchronous first listing so restore can resolve by label before the window settles):

```python
        self._update_drives(list_usb_drives())
        self._restore_locations()
        self.root.after(3000, self._poll_drives)
```

(This replaces the `self._refresh_drives()` line added in Task 7 Step 1; the 3-second poll stays.)

- [ ] **Step 3: Record + persist on run start**

In `_start_run`, right after the `files_to_process` empty-check (`return` at ~line 1586) and before `total_size = ...`:

```python
        self._record_locations()
        save_profiles_to_disk(self.profiles, self.current_profile.get(),
                              self.last_locations)
```

- [ ] **Step 4: Record on close**

In `_on_close`, before the existing `save_profiles_to_disk(...)` call:

```python
        self._record_locations()
```

- [ ] **Step 5: Remove the temporary hasattr guard from Task 7**

In `_update_drives`, replace:

```python
        if hasattr(self, "_try_pending_restore"):
            self._try_pending_restore()
```

with:

```python
        self._try_pending_restore()
```

- [ ] **Step 6: Verify (tests + compile + manual end-to-end)**

Run: `python -m unittest tests.test_organizer -v`
Expected: `OK`, 24 tests passed.

Run: `python -m py_compile Organizer_Linux.py && echo COMPILE-OK`
Expected: `COMPILE-OK`

Manual end-to-end (needs the desktop session and a USB stick):
1. Launch `python Organizer_Linux.py`, pick a USB drive as source (via the picker) and a folder as dest, run a small copy. Close the app.
2. Check `cat ~/.photo_organizer/profiles.json` — a `last_locations` object with `source` (with `label`/`rel`) and `dest` exists.
3. Relaunch — both paths are restored.
4. Close, unplug the stick, relaunch — source field is empty with the hint `Last used drive '<LABEL>' not connected.`
5. Plug the stick back in — within ~3 s the source path auto-fills and the hint turns into `✓ <LABEL> reconnected` in green.

---

## Final acceptance sweep

- [ ] `python -m unittest tests.test_organizer -v` → `OK` (24 tests)
- [ ] `python -m py_compile Organizer_Linux.py` → clean
- [ ] Launch the app once: USB rows present, sync checkbox present and checked, a small copy run to a USB stick logs `sync writes: on` and completes with true device throughput.
