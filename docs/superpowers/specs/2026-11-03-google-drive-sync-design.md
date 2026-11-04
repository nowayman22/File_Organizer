# Google Drive Sync — Design

**Date:** 2026-11-03
**Status:** Implemented (transport, push/pull workers, UI, docs)
**Base:** `File_Organizer.py`

## Goal

Let the organizer **push** a local folder to Google Drive and **pull** a Drive folder
back down, reusing the existing sort modes, duplicate handling, verification and
logging — so "destination" and "source" can each be either a local folder or a Drive
folder.

## Background

The app is single-file Tkinter, stdlib + optional Pillow, ~3,500 lines. It already
shells out to system tools for hardware concerns: `lsblk` for drive discovery and
`udisksctl` for mount/unmount/format. Sorting, duplicates, verification and progress
are transport-agnostic helpers today:

- `build_target_subfolder(file, ext, sort_mode)` → relative subfolder
- `get_photo_date(path)` → EXIF → MP4 `mvhd` → filesystem mtime
- `unique_destination(path)`, `files_identical(src, dst, verify)`
- `verify_copy(src, dst, mode)`, `sha256_of`, `human_size`, progress via
  `msg_queue` and `cancel_event`

## Decisions

### 1. Transport: the `rclone` CLI (not an in-app OAuth client)

`rclone` v1.75.1 is already installed on the target machine; no Python Google
libraries are present. Using it:

- keeps the app **stdlib-only** — no new Python dependency;
- matches the existing `lsblk`/`udisksctl` pattern;
- delegates OAuth, token refresh, resumable uploads, retry/backoff and Drive rate
  limits to a tested tool.

Authentication is a **one-time user step**: `rclone config` → remote named e.g.
`gdrive`. The app never stores tokens or client secrets.

Rejected: `google-api-python-client` (+ its dependency tree, breaking stdlib-only);
a hand-rolled OAuth + REST client (~600 lines, still needs a Google Cloud project).

### 2. Addressing: remote specs wherever a folder path is accepted

A folder string matching `name:path` where `name` is a **configured** rclone remote
is treated as a Drive path: `gdrive:FileOrganizer/Photos`. Requiring a configured
remote means ordinary names such as `notes: 2024` are still local paths.

This needs **no new widgets**: the source and destination path entries already exist,
profiles persist paths as strings, and the drive picker lists remotes alongside USB
drives, and remotes are managed in the **Config** tab (add/test/remove, default
folder). When a field already holds a remote path, its browse button opens a **folder
chooser** (double-click to descend, ⬆ to ascend, *Use this folder* to select); folders
appear on upload, so a not-yet-existing path is selectable and reports that it will be
created.

Rejected: a separate Drive panel / backend dropdown — more UI, more state to keep in
sync with profiles, no extra capability.

### 3. Operation semantics map 1:1 onto the existing dropdown

| Operation | local → Drive (push) | Drive → local (pull) |
|---|---|---|
| Copy | upload, keep local | download, keep remote |
| Move | upload, verify, delete local | download, verify, delete remote |

Duplicate modes apply per transport:

- **push** — `skip` (remote exists), `overwrite` (`rclone copyto` replaces),
  `rename` (`name_2.ext` on the remote), `erase` (delete the *local* source when the
  remote copy is identical).
- **pull** — unchanged local behaviour (`rename` / `overwrite` / `skip`); `erase`
  deletes the remote file when the local copy is identical.

### 4. Verification

Drive reports an MD5 per binary object, so the existing three modes degrade cleanly:

| Mode | local↔local | local↔Drive |
|---|---|---|
| off | no check | no check |
| quick | size + mtime | size |
| hash | SHA-256 | **size + MD5** (rclone-reported) |

Google-native documents (Docs/Sheets/Slides) expose no binary MD5; those verify by
size only and log a note once. Exporting native docs is out of scope.

### 5. Date sorting on pull

Remote entries carry `ModTime` from `rclone lsjson`. That timestamp drives date-based
sort modes (the remote equivalent of mtime). EXIF/MP4 dates would require downloading
first; out of scope for v1.

### 6. Safety

- `join_remote()` refuses `..` traversal and non-spec bases.
- The app only ever uploads to computed paths, and deletes exactly the file it
  transferred (`rclone deletefile`); it never deletes a directory or syncs deletions.
- Cancellation terminates the running rclone process; no partial remote state is
  cleaned up automatically (a later rename/overwrite resolves it).
- `rclone` absence is detected up front and reported as an actionable message, not a
  stack trace.

## Interfaces (backend — landed)

```python
RemoteEntry(path: str, size: int, modtime: datetime|None, md5: str|None)  # .name, .suffix
rclone_bin() -> str|None
run_rclone(args, timeout=120) -> (rc, stdout, stderr)
list_remotes() -> list[str]
split_remote_spec(spec) -> (remote, path)|None
is_remote_path(spec, remotes=None) -> bool
join_remote(spec, *parts) -> str          # raises ValueError on traversal
scan_remote(spec, recursive=True) -> list[RemoteEntry]   # RuntimeError on failure
remote_stat(spec) -> RemoteEntry|None
upload_file(local, spec) -> (ok, err)
download_file(spec, local) -> (ok, err)
delete_remote_file(spec) -> (ok, err)
remote_quota(remote) -> dict|None
md5_of(path) -> str
remote_verify(local, spec, mode) -> (ok, reason)
```

Environment override `FILE_ORGANIZER_RCLONE` points at an alternate binary (used by
tests, and by users with a non-PATH rclone).

## Out of scope

- In-app OAuth / token storage; Google Docs export; sharing or permission management.
- Bidirectional mirroring or delete propagation.
- Parallel transfers (rclone's own `--transfers` is not used; one file at a time keeps
  progress and cancellation simple).
- Shared drives / team-drive selection beyond what `rclone config` exposes.

## Error handling

- Missing rclone → one error at validation time: "rclone not installed — see README".
- Remote not configured → "remote 'foo' is not configured (rclone config)".
- Per-file transfer failures are logged, counted, and the run continues; the summary
  reports errors and verification failures separately.
- `rclone lsjson` failure on scan aborts the scan with the rclone message.

## Tests

The fake-rclone harness (`FAKE_RCLONE_SOURCE` in `tests/test_organizer.py`) implements
`listremotes`, `lsjson`, `copyto`, `deletefile` and `about` against a temp directory,
so paths, hashes, transfers and error paths are exercised without network or
credentials.
