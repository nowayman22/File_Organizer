# File Organizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Photo Organizer into a File Organizer that scans and organizes every ordinary file type, fully rebranded.

**Architecture:** Keep the single-file Tkinter app. Replace the media-only scanner (`walk_media`) with one that yields all ordinary files (skipping hidden files/dirs and system junk); keep EXIF/MP4 embedded-date extraction for photos/videos with filesystem-mtime fallback for everything else; broaden type-folder auto-detection; re-brand the app; rename the module and update the tests to match.

**Tech Stack:** Python 3.9+ stdlib, Tkinter, `unittest`, optional Pillow (EXIF only).

## Global Constraints

- Python 3.9+, standard library only (plus optional Pillow for EXIF).
- "All ordinary files" = every extension **and** extension-less regular files; still skip hidden files/dirs (leading `.`) and system junk dirs: `System Volume Information`, `$RECYCLE.BIN`, `__MACOSX`, `.Spotlight-V100`, `.Trashes`, `lost+found`.
- Keep `IMAGE_EXTENSIONS`, `VIDEO_EXTENSIONS`, `MP4_LIKE_EXTENSIONS` — they power embedded-date extraction. Remove the now-unused `MEDIA_EXTENSIONS`.
- Do not change any transfer / verify / duplicate / USB / profile / theme behavior.
- Full rebrand: window title, header label, docstring, class name, session-log header, WM_CLASS, and `PROFILES_DIR` → `~/.file_organizer`.
- Rename script `Organizer_Linux.py` → `File_Organizer.py`; tests import `File_Organizer as org`.
- Tests run from project root: `python -m unittest tests.test_organizer -v`.

---

### Task 1: Scanner walks all ordinary files

Replace the media-only scanner with one that yields every ordinary file.

**Files:**
- Modify: `Organizer_Linux.py:1299-1324` (`walk_media` → `walk_files`), `:418` (remove `MEDIA_EXTENSIONS`), `:402` (section comment), `:2480` (caller in `_scan_worker`), `:2506` and `:2523` (UI strings)
- Test: `tests/test_organizer.py` (new `WalkFilesTests` class)

**Interfaces:**
- Produces: `walk_files(source: Path, recurse: bool, cancel_event: threading.Event) -> Iterator[Path]` — yields every ordinary file under `source`; skips hidden files/dirs and system junk dirs.
- Consumes: `self.cancel_event` in `_scan_worker` (unchanged usage).

- [ ] **Step 1: Add failing tests**

Append this class to `tests/test_organizer.py`:

```python
class WalkFilesTests(unittest.TestCase):
    def test_yields_any_extension_and_extensionless(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "report.pdf").write_bytes(b"pdf")
            (src / "song.mp3").write_bytes(b"mp3")
            (src / "README").write_bytes(b"no ext")
            cancel = threading.Event()
            found = sorted(p.name for p in org.walk_files(src, True, cancel))
            self.assertEqual(found, ["README", "report.pdf", "song.mp3"])

    def test_skips_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / ".env").write_bytes(b"x")
            (src / "keep.txt").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, True, cancel)]
            self.assertEqual(found, ["keep.txt"])

    def test_skips_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            hid = src / ".thumbnails"
            hid.mkdir()
            (hid / "t.png").write_bytes(b"x")
            (src / "ok.png").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, True, cancel)]
            self.assertEqual(found, ["ok.png"])

    def test_skips_system_junk_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            junk = src / "System Volume Information"
            junk.mkdir()
            (junk / "data.bin").write_bytes(b"x")
            (src / "good.txt").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, True, cancel)]
            self.assertEqual(found, ["good.txt"])

    def test_non_recursive_only_top_level_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            sub = src / "sub"
            sub.mkdir()
            (sub / "deep.txt").write_bytes(b"x")
            (src / "top.txt").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, False, cancel)]
            self.assertEqual(found, ["top.txt"])

    def test_cancel_stops_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            for i in range(5):
                (src / f"f{i}.txt").write_bytes(b"x")
            cancel = threading.Event()
            cancel.set()
            found = list(org.walk_files(src, True, cancel))
            self.assertEqual(found, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.WalkFilesTests -v`
Expected: FAIL with `AttributeError: module 'Organizer_Linux' has no attribute 'walk_files'`

- [ ] **Step 3: Replace `walk_media` with `walk_files`**

In `Organizer_Linux.py`, replace the whole `walk_media` function (lines 1299-1324) with:

```python
# System-junk directories we never descend into.
SKIPPED_DIRS = {"System Volume Information", "$RECYCLE.BIN", "__MACOSX",
                ".Spotlight-V100", ".Trashes", "lost+found"}


def walk_files(source: Path, recurse: bool, cancel_event: threading.Event):
    """Yield every ordinary file under `source`.

    Skips hidden files/dirs (leading '.') and system junk directories.
    Resilient against permission errors.
    """
    if recurse:
        for root, dirs, files in os.walk(source, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in SKIPPED_DIRS]
            if cancel_event.is_set():
                return
            for name in files:
                if name.startswith("."):
                    continue
                yield Path(root) / name
    else:
        try:
            for p in source.iterdir():
                if cancel_event.is_set():
                    return
                if p.is_file() and not p.name.startswith("."):
                    yield p
        except (PermissionError, OSError):
            return
```

- [ ] **Step 4: Update the caller and remove `MEDIA_EXTENSIONS`**

In `_scan_worker` (`Organizer_Linux.py:2480`), change:

```python
            for p in walk_media(source, recurse, self.cancel_event):
```
to:
```python
            for p in walk_files(source, recurse, self.cancel_event):
```

Remove the now-unused constant at line 418:
```python
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
```

Update the section comment at line 402 from `#  Media extensions` to `#  Extensions (used for embedded-date extraction)`.

- [ ] **Step 5: Update scan-summary UI strings**

At `Organizer_Linux.py:2506`, change:
```python
            f"Scan complete: {total:,} media files, {human_size(total_size)} "
```
to:
```python
            f"Scan complete: {total:,} files, {human_size(total_size)} "
```

At `Organizer_Linux.py:2523`, change:
```python
            ttk.Label(self.types_inner, text="No media files found.",
```
to:
```python
            ttk.Label(self.types_inner, text="No files found.",
```

- [ ] **Step 6: Run full test suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: ALL PASS (new `WalkFilesTests` plus all pre-existing tests).

- [ ] **Step 7: Commit**

```bash
git add Organizer_Linux.py tests/test_organizer.py
git commit -m "feat: scan and organize all ordinary file types, not just media"
```

---

### Task 2: Broaden type-folder auto-detection

Extend `KNOWN_TYPE_FOLDERS` so destination-structure detection recognizes non-media type folders.

**Files:**
- Modify: `Organizer_Linux.py:463-469` (`KNOWN_TYPE_FOLDERS`)
- Test: `tests/test_organizer.py` (new `DetectStructureTests` class)

**Interfaces:**
- Consumes: `detect_folder_structure(dest: Path) -> Optional[str]` (already exists, line 1232).
- Produces: an expanded `KNOWN_TYPE_FOLDERS` set; `detect_folder_structure` returns `"file_type"` when a destination contains recognized type-named folders and no year/date folders dominate.

- [ ] **Step 1: Add failing test**

Append this class to `tests/test_organizer.py`:

```python
class DetectStructureTests(unittest.TestCase):
    def test_detects_non_media_type_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "PDF").mkdir()
            (dest / "ZIP").mkdir()
            self.assertEqual(org.detect_folder_structure(dest), "file_type")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_organizer.DetectStructureTests -v`
Expected: FAIL (returns `None`; `PDF`/`ZIP` not yet in `KNOWN_TYPE_FOLDERS`).

- [ ] **Step 3: Expand `KNOWN_TYPE_FOLDERS`**

In `Organizer_Linux.py`, change the `KNOWN_TYPE_FOLDERS` set (lines 463-469) to:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_organizer -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add Organizer_Linux.py tests/test_organizer.py
git commit -m "feat: recognize non-media type folders during structure auto-detection"
```

---

### Task 3: Verify mtime fallback for non-media files

Characterization test locking in that date sorting works for files with no embedded date.

**Files:**
- Test: `tests/test_organizer.py` (new `GetPhotoDateTests` class)

**Interfaces:**
- Consumes: `get_photo_date(path: Path) -> datetime` (already exists, line 1044).

- [ ] **Step 1: Add the test**

Append this class to `tests/test_organizer.py`:

```python
class GetPhotoDateTests(unittest.TestCase):
    def test_non_media_file_uses_filesystem_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "notes.txt"
            f.write_bytes(b"hi")
            os.utime(f, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
            self.assertAlmostEqual(
                org.get_photo_date(f).timestamp(), 1_700_000_000.0)
```

- [ ] **Step 2: Run the test**

Run: `python -m unittest tests.test_organizer.GetPhotoDateTests -v`
Expected: PASS (behavior already implemented via mtime fallback; no source change needed).

- [ ] **Step 3: Commit**

```bash
git add tests/test_organizer.py
git commit -m "test: non-media files sort by filesystem mtime"
```

---

### Task 4: Re-brand the app

Change every user-facing and code identifier from "Photo Organizer" to "File Organizer", and move the profiles dir.

**Files:**
- Modify: `Organizer_Linux.py` (many small edits, listed below)

**Interfaces:**
- Produces: module-level `PROFILES_DIR = Path.home() / ".file_organizer"`, class `FileOrganizerApp`, WM_CLASS `FileOrganizer`.
- Consumes: nothing new — tests patch `org.PROFILES_DIR` / `org.PROFILES_FILE` by attribute name, so the value change is safe.

- [ ] **Step 1: Update the module docstring**

In `Organizer_Linux.py`, change lines 2-6:

```python
"""
Photo Organizer (v3)
====================
A modern GUI application to copy or move photos/videos from a source drive
or folder to a destination, sorted into folders by date or file type.
```
to:
```python
"""
File Organizer
==============
A modern GUI application to copy or move files of any type from a source
drive or folder to a destination, sorted into folders by date or file type.
```

In the same docstring, change the Run section (line 67):
```python
    python Organizer.py
```
to:
```python
    python File_Organizer.py
```

- [ ] **Step 2: Move the profiles directory**

At `Organizer_Linux.py:475`, change:
```python
PROFILES_DIR  = Path.home() / ".photo_organizer"
```
to:
```python
PROFILES_DIR  = Path.home() / ".file_organizer"
```

- [ ] **Step 3: Update `get_photo_date` docstring**

At `Organizer_Linux.py:1045`, change:
```python
    """Best-effort capture date for a photo OR video file.
```
to:
```python
    """Best-effort capture date for any file.

    Photos may have EXIF, MP4-family videos may have an embedded 'mvhd'
    creation time; everything else falls back to filesystem mtime.
```

- [ ] **Step 4: Rename the app class and its instantiations**

At `Organizer_Linux.py:1512`, change:
```python
class PhotoOrganizerApp:
```
to:
```python
class FileOrganizerApp:
```

At `Organizer_Linux.py:3281`, change:
```python
    PhotoOrganizerApp(root)
```
to:
```python
    FileOrganizerApp(root)
```

- [ ] **Step 5: Update window title, header, subtitle, and log strings**

At `Organizer_Linux.py:1520`:
```python
        self.root.title("Photo Organizer")
```
→
```python
        self.root.title("File Organizer")
```

At `Organizer_Linux.py:1583`:
```python
        self._log_header(f"Photo Organizer started  •  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
```
→
```python
        self._log_header(f"File Organizer started  •  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
```

At `Organizer_Linux.py:1610`:
```python
        ttk.Label(header, text="📷  Photo Organizer", style="Title.TLabel").pack(side=tk.LEFT)
```
→
```python
        ttk.Label(header, text="🗂  File Organizer", style="Title.TLabel").pack(side=tk.LEFT)
```

At `Organizer_Linux.py:1611-1614`, change the two subtitle lines:
```python
        if PIL_AVAILABLE:
            subtitle = "EXIF + MP4/MOV date detection"
        else:
            subtitle = "MP4/MOV dates OK  •  pip install Pillow for photo EXIF"
```
→
```python
        if PIL_AVAILABLE:
            subtitle = "EXIF + MP4/MOV dates for photos & videos"
        else:
            subtitle = "Dates via file time  •  pip install Pillow for EXIF"
```

At `Organizer_Linux.py:2434`:
```python
                text="No existing photo-library structure detected.",
```
→
```python
                text="No existing folder structure detected.",
```

At `Organizer_Linux.py:3153`:
```python
                f"\n=== Photo Organizer session log — "
```
→
```python
                f"\n=== File Organizer session log — "
```

At `Organizer_Linux.py:3279`:
```python
    root = tk.Tk(className="PhotoOrganizer")   # WM_CLASS for the .desktop entry
```
→
```python
    root = tk.Tk(className="FileOrganizer")    # WM_CLASS for the .desktop entry
```

- [ ] **Step 6: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: ALL PASS (no test references any renamed identifier).

- [ ] **Step 7: Commit**

```bash
git add Organizer_Linux.py
git commit -m "feat: re-brand Photo Organizer as File Organizer"
```

---

### Task 5: Rename the script file and update tests

Move `Organizer_Linux.py` → `File_Organizer.py` and point the test suite at the new module name.

**Files:**
- Rename: `Organizer_Linux.py` → `File_Organizer.py`
- Modify: `tests/test_organizer.py:1`, `:15`, `:510`

**Interfaces:**
- Consumes: everything produced in Tasks 1-4 (now living in `File_Organizer.py`).
- Produces: module `File_Organizer`; tests import `File_Organizer as org`.

- [ ] **Step 1: Rename the script with git mv**

Run:
```bash
git mv Organizer_Linux.py File_Organizer.py
```

- [ ] **Step 2: Update the test module docstring**

At `tests/test_organizer.py:1`, change:
```python
"""Unit tests for the pure helper functions in Organizer_Linux.py.
```
to:
```python
"""Unit tests for the pure helper functions in File_Organizer.py.
```

- [ ] **Step 3: Update the import**

At `tests/test_organizer.py:15`, change:
```python
import Organizer_Linux as org
```
to:
```python
import File_Organizer as org
```

- [ ] **Step 4: Update the persistence-test fixture path**

At `tests/test_organizer.py:510`, change:
```python
        pdir = Path(tmp) / ".photo_organizer"
```
to:
```python
        pdir = Path(tmp) / ".file_organizer"
```

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: ALL PASS (including `WalkFilesTests`, `DetectStructureTests`, `GetPhotoDateTests`).

- [ ] **Step 6: Smoke-check the module imports**

Run: `python -c "import File_Organizer; print('ok')"`
Expected: prints `ok` (module-level imports only; no Tk widgets are created at import time).

- [ ] **Step 7: Commit**

```bash
git add File_Organizer.py tests/test_organizer.py
git commit -m "refactor: rename script Organizer_Linux.py to File_Organizer.py"
```

---

### Task 6: Final verification

Confirm the whole change set is green and nothing references the old name.

**Files:** none changed (verification only).

- [ ] **Step 1: Full test suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: ALL PASS.

- [ ] **Step 2: Confirm no stale references**

Run:
```bash
grep -rn "Organizer_Linux\|Photo Organizer\|photo_organizer\|PhotoOrganizer\|walk_media\|MEDIA_EXTENSIONS\|photo-library" --include="*.py" --include="*.md" .
```
Expected: no matches (only the historical docs under `docs/superpowers/plans/2026-07-*` and `docs/superpowers/specs/2026-07-*` describe the old name — that history is intentional and left as-is).

- [ ] **Step 3: Compile check**

Run: `python -m py_compile File_Organizer.py`
Expected: exit 0, no output.

- [ ] **Step 4: Confirm clean tree and commit**

Run: `git status --short`
Expected: nothing to commit (clean). If Task 6's grep in Step 2 rewrote any file, commit it here.

---

## Self-Review Notes

- **Spec coverage:** §1 copy → already done (commit `6aadda6`). §2 scanner → Task 1. §3 mtime fallback → Task 3. §4 type folders → Task 2. §5 UI copy → Tasks 1 & 4. §6 rebrand → Tasks 4 & 5. §7 tests/verification → Tasks 1-3, 6.
- **No placeholders:** every code step shows exact before/after text; every command shows expected output.
- **Type consistency:** `walk_files(source, recurse, cancel_event)` name/signature used consistently in Task 1's test, implementation, and caller. `KNOWN_TYPE_FOLDERS` referenced only in `detect_folder_structure`. `FileOrganizerApp` referenced in `main()` alongside its class def in Task 4.

---

## Post-implementation corrections

Recorded on branch `fix/pre-feature-cleanup` after Tasks 1-6 shipped. The steps above are
left as written (they are the historical record); these are the places where reality
differed.

1. **Task 6, Step 2 expected output was wrong.** The suggested grep reports *no* matches,
   but run over the repo it finds ~120 matches in `docs/` — including this plan and the
   2026-08-10 design spec themselves — plus deliberate code references. What actually
   holds is that no *obsolete identifier* survives:

   ```bash
   grep -rn "Organizer_Linux\|walk_media\|MEDIA_EXTENSIONS\|PhotoOrganizer" File_Organizer.py tests/test_organizer.py
   # -> no matches (exit 1)
   ```

   The remaining "Photo Organizer" / `.photo_organizer` mentions are intentional: the
   legacy-store migration added in correction 4, and
   `test_removes_stale_photo_organizer_entry`, which asserts that `install.sh` deletes the
   stale `photo-organizer.desktop` launcher.

2. **Task 4 missed a docstring line.** The module docstring's profile note
   (`to ~/.photo_organizer/profiles.json`) is not in any Task 4 step. It was corrected to
   `~/.file_organizer` during implementation.

3. **Task 1's banner edit left the comment misaligned.** Only the text of the fixed-width
   `#  Media extensions … #` rule was replaced, so the trailing `#` no longer lined up;
   the line was re-padded during implementation.

4. **The `PROFILES_DIR` move silently dropped user state** (not addressed by any task).
   `profiles.json` also stores the remembered source/destination locations, so the rename
   abandoned both. Fixed by a one-time migration: `migrate_legacy_profiles()` copies
   `~/.photo_organizer/profiles.json` into the new store when — and only when — the new
   store does not exist yet; it validates the JSON first, never overwrites, and never
   deletes the legacy file. Covered by `LegacyProfileMigrationTests`.

5. **"Every ordinary file" had no dependency-tree filter.** A general-purpose organizer
   would otherwise copy `node_modules/`, `venv/`, and `__pycache__/` wholesale, plus
   `Thumbs.db` / `desktop.ini`. `SKIPPED_DIRS` and `SKIPPED_FILES` now exclude them; see
   `test_skips_dependency_trees` and `test_skips_os_metadata_files`.
