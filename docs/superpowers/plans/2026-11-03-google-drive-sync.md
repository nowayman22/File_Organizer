# Google Drive Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push a local folder to Google Drive and pull a Drive folder to local, reusing the
existing sort/duplicate/verify pipeline.

**Architecture:** A thin adapter (`scan_remote`, `upload_file`, `download_file`,
`remote_verify`, …) wraps the `rclone` CLI. Remote folders are addressed as `remote:path`
where `remote` is a configured rclone remote, so the existing path entries, profiles and
drives picker carry Drive targets without new widgets. `RemoteEntry` stands in for `Path`
in the scan list; pull workers sort by the remote `ModTime` instead of mtime.

**Tech Stack:** Python 3.9+ stdlib, Tkinter, `unittest`, `rclone` CLI (external),
fake-rclone stub for tests.

## Global Constraints

- No new Python dependencies; `rclone` is the only external requirement.
- Authentication is the user's one-time `rclone config`; the app stores no tokens.
- Drive → Drive runs are rejected with a clear message (v1).
- A remote path is only a Drive path when its prefix is a *configured* rclone remote.
- Existing local↔local behaviour, profiles, and USB/theme features must not change.
- Verification: `off`/`quick` unchanged; `hash` on Drive means size + rclone MD5.
- Tests never touch the network or real Drive; use the fake-rclone harness.

---

### Task 1: rclone transport layer — DONE

**Status:** landed on `feature/google-drive` with `RcloneBackendTests` (13 tests).

**Produces:** `RemoteEntry`, `rclone_bin`, `run_rclone`, `list_remotes`,
`split_remote_spec`, `is_remote_path`, `join_remote`, `scan_remote`, `remote_stat`,
`upload_file`, `download_file`, `delete_remote_file`, `remote_quota`, `md5_of`,
`remote_verify`; `FILE_ORGANIZER_RCLONE` override.

---

### Task 2: Remote-aware scanning

**Files:** `File_Organizer.py` (`_start_scan`, `_scan_worker`), tests.

**Interfaces:**
- Consumes: `is_remote_path`, `scan_remote`, `RemoteEntry`.
- Produces: scan list containing `Path` (local) or `RemoteEntry` (Drive) objects, both
  exposing `.name`, `.suffix`; `self.scanned_files` becomes `list[Path | RemoteEntry]`.

- [ ] **Step 1: Add failing tests** — `scan_remote` already returns entries; the new
  behaviour is selection logic. Extract it as a pure helper so it is testable:

```python
def remote_entries_of(files):
    """True when a scan list holds remote entries (mixed lists are a bug)."""
    return bool(files) and all(isinstance(f, RemoteEntry) for f in files)
```

  Tests: `remote_entries_of([]) is False`; `remote_entries_of([RemoteEntry(...)])` true;
  `remote_entries_of([Path("a"), RemoteEntry(...)])` false.

- [ ] **Step 2: Validate the source in `_start_scan`**

```python
src = self.source_path.get().strip()
if is_remote_path(src):
    pass                          # reachability errors surface from the worker
elif not src or not Path(src).is_dir():
    messagebox.showerror("Invalid source",
                         "Please choose a valid source folder or drive.")
    return
```

- [ ] **Step 3: Branch `_scan_worker`**

```python
if is_remote_path(str(source)):
    entries = scan_remote(str(source), recurse)
    for e in entries:
        files.append(e)
        counts[e.suffix.lower()] += 1
        sizes[e.suffix.lower()] += e.size
else:
    for p in walk_files(source, recurse, self.cancel_event):
        ... existing body ...
```

- [ ] **Step 4: Run the suite** — `python3 -m unittest tests.test_organizer -v`; existing
  tests plus the new helper test pass.
- [ ] **Step 5: Commit** — `feat: scan a Google Drive folder as the source`.

---

### Task 3: Push worker (local → Drive)

**Files:** `File_Organizer.py`, tests.

**Interfaces:**
- Produces pure helpers (unit-tested):
  - `remote_target(dest_spec, subfolder: Path, name: str) -> str`
  - `remote_plan_renames(target_spec, probe, limit=999) -> str|None` where `probe(spec)`
    returns truthy when taken (inject `remote_stat` for tests).
  - `remote_matches(local: Path, spec: str, verify: str) -> bool` (size + optional MD5).
- Consumes: `build_target_subfolder`, `upload_file`, `remote_verify`,
  `delete_remote_file` (not used by push).

- [ ] **Step 1: Failing tests** for the three helpers: subfolder joining keeps POSIX
  separators; `_2`/`_3` probing stops at the first free name and gives up a `None`; a
  same-size/different-bytes remote is *not* a match under `hash`, but is under `quick`.
- [ ] **Step 2: Implement helpers** (pure, no Tk).
- [ ] **Step 3: Worker** `_push_worker(files, dest_spec, op, sort_key, dup, verify)` —
  mirrors `_transfer_worker`: per file compute subfolder, resolve duplicates against
  `remote_stat`, `upload_file`, then `remote_verify`; on `op == "move"` or
  `run_mode == "transfer_verify"` delete the local source only after a passing verify;
  on failed verify delete the remote copy (`delete_remote_file`) and keep the source.
  Progress via `self._tick_progress`; cancellation checked each iteration.
- [ ] **Step 4: Dispatch** in `_start_run` when the destination is remote:

```python
src_remote = is_remote_path(src_text)
dst_remote = is_remote_path(dst_text)
if src_remote and dst_remote:
    messagebox.showerror("Unsupported", "Drive → Drive is not supported yet.")
    return
```

- [ ] **Step 5: Run suite; commit** — `feat: push files to a Google Drive folder`.

---

### Task 4: Pull worker (Drive → local)

**Files:** `File_Organizer.py`, tests.

**Interfaces:**
- Produces:
  - `subfolder_for_date(dt: datetime, ext: str, sort_mode: str) -> Path` extracted from
    `build_target_subfolder`, which becomes
    `subfolder_for_date(get_photo_date(file_path), ext, sort_mode)` (behaviour-preserving;
    existing tests must stay green).
  - `pull_target(dest_root: Path, entry: RemoteEntry, sort_mode: str) -> Path` using
    `entry.modtime` (fallback: `datetime.now()`).
- Consumes: `download_file`, `remote_verify`, `unique_destination`, `delete_remote_file`.

- [ ] **Step 1: Failing tests** — `subfolder_for_date` reproduces every sort mode for a
  fixed date (characterization of the refactor: compare against
  `build_target_subfolder` on a temp file whose mtime is pinned); `pull_target` maps a
  `RemoteEntry` into the expected `dest/2024/01-January/x.jpg`.
- [ ] **Step 2: Refactor + implement `pull_target`.**
- [ ] **Step 3: Worker** `_pull_worker(entries, dest_root, op, sort_key, dup, verify)` —
  local duplicate rules as today (`skip`/`overwrite`/`rename`/`erase`); `erase` deletes
  the *remote* file when the local copy is identical; `op == "move"` deletes the remote
  file after a passing `remote_verify`; failed verification removes the partial local
  file and keeps the remote one.
- [ ] **Step 4: Run suite; commit** — `feat: pull a Google Drive folder into a destination`.

---

### Task 5: UI wiring

**Files:** `File_Organizer.py` (`_browse_*`, `_update_drives`, `_pick_drive`,
`_start_run`, `_handle_verify_done`, log/status strings).

- [ ] **Step 1:** `_update_drives` appends configured remotes to both drive pickers as
  `☁ gdrive:` entries, so a Drive folder can be chosen without typing.
- [ ] **Step 2:** `_browse_source`/`_browse_dest` refuse to open a local dialog when the
  field holds a remote spec; they explain the `rclone config` workflow instead.
- [ ] **Step 3:** Remote-aware verification: `_verify_remote_worker` compares each planned
  target (push) or each remote entry against the destination (pull) with size/MD5 and
  reuses the existing summary dialog, labelled "present / missing on Drive".
- [ ] **Step 4:** Preflight in `_start_run`: missing rclone → one actionable error;
  unconfigured remote → name it. Format-source stays disabled when the destination is
  remote.
- [ ] **Step 5:** Update the subtitle/hints to mention Drive paths; keep the window
  layout unchanged.
- [ ] **Step 6:** Run the suite headless; commit — `feat: address Drive folders in the
  source and destination fields`.

---

### Task 6: Docs and verification

- [ ] **Step 1:** README: "Google Drive" section — `rclone config`, path form
  (`gdrive:FileOrganizer`), operation/duplicate/verify semantics table, one-file-at-a-time
  note, cancellation behaviour.
- [ ] **Step 2:** `python3 -m unittest tests.test_organizer -v` and
  `python3 -m py_compile File_Organizer.py`.
- [ ] **Step 3:** Manual smoke (requires a configured remote): scan a small Drive folder,
  pull it, push it back, verify, then `erase` a duplicate pair.
- [ ] **Step 4:** Commit and push the branch; open the PR into `main`.

---

## Self-Review Notes

- **Spec coverage:** transport (§1) → Task 1 done; addressing (§2) → Tasks 2/5; operation
  semantics (§3) → Tasks 3/4; verification (§4) → Tasks 1/3/4; date sorting (§5) → Task 4;
  safety (§6) → Tasks 1/3/4; tests → every task.
- **No placeholder code:** helpers are named, typed and unit-tested; workers are thin
  loops over them.
- **Risk:** workers cannot be unit-tested directly (they need Tk); pushing logic into pure
  helpers keeps coverage high and the workers mechanical.
