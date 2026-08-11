# USB-Aware Locations & Linux Optimizations — Design

**Date:** 2026-07-14
**Target:** `Organizer_Linux.py` (single-file Tkinter app, stdlib-only)
**Primary use case:** transferring media from one USB device to another on Linux (Arch; drives auto-mounted by udisks under `/run/media/<user>/<LABEL>`).

## Goals

1. Remember the last-used source and destination locations across sessions, in a way that survives USB drives being unplugged and remounted (possibly at a different mountpoint).
2. Make mounted USB drives one-click selectable in the UI.
3. Make transfers to USB devices safe: data must be physically on the device before a copy is reported as done (and, in move mode, before the source is deleted); hash verification must read from the device, not the RAM page cache.
4. Modest transfer-speed tuning without adding dependencies.

Non-goals: udisks/D-Bus integration, auto-mounting drives, changes to sorting/dedupe/verify semantics, multi-file refactor. The app stays a single stdlib-only file.

## 1. Drive detection (lsblk poller)

New module-level code near the existing helpers:

- `UsbDrive` — a small dataclass-like record: `label`, `mountpoint` (Path), `size` (bytes), `free` (bytes), `device` (e.g. `sda1`).
- `list_usb_drives() -> list[UsbDrive]`:
  - Runs `lsblk -J -b -o NAME,LABEL,MOUNTPOINT,SIZE,FSAVAIL,RM,TRAN,TYPE` with a short timeout (2 s).
  - Walks the JSON tree (children included). A node qualifies if it has a non-empty `mountpoint` AND (its own or any ancestor's `tran == "usb"` OR `rm == true`). This covers USB sticks, USB card readers, and SD card slots.
  - Excludes mountpoints in a denylist: `/`, `/boot`, `/boot/efi`, `/home`, `/var`, anything under `/snap`.
  - Label falls back to the last mountpoint component, then to the device name.
  - On any failure (`FileNotFoundError`, timeout, JSON error), falls back to `_scan_media_dirs()`: listing directories under `/run/media/<user>/` and `/media/<user>/`, with size/free from `os.statvfs` and label = directory name.
  - Never raises; returns `[]` at worst.

Polling: the app schedules a poll every 3000 ms with `root.after`. Each poll runs `list_usb_drives()` in a short-lived daemon thread and posts `("drives", drives)` to the existing `self.msg_queue`; `_poll_queue` handles it. The UI is updated only when the drive list differs from the previous one (compare `(label, str(mountpoint), free)` tuples — free-space changes update the labels too, but combobox selection is preserved by matching mountpoints).

## 2. Quick-pick UI

In both the Source and Destination cards, a new row under the path entry:

```
USB:  [ SANDISK — 57.6 GB (12.1 GB free) ▾ ]  ⟳
```

- Read-only `ttk.Combobox` listing current drives, formatted `LABEL — <size> (<free> free)` via the existing `human_size`.
- Selecting an entry sets the corresponding path variable to the drive's mountpoint (and logs a DEBUG line, like `_browse_source` does).
- The ⟳ button triggers an immediate re-poll.
- When no drives are present, the combobox shows placeholder text `no USB drives detected` and is disabled.
- Setting the path via the combobox, Browse, or typing all flow through the same `StringVar`, so location recording (§3) hooks the variable writes, not the widgets.

## 3. Device-aware location memory

Persistence lives in the existing `~/.photo_organizer/profiles.json`, in a new top-level key (ignored by old versions of the app, and absent keys are tolerated by the loader):

```json
"last_locations": {
  "source": {"path": "/run/media/nikits/SANDISK/DCIM", "label": "SANDISK", "rel": "DCIM"},
  "dest":   {"path": "/run/media/nikits/BACKUP",       "label": "BACKUP",  "rel": "."}
}
```

- **Recording.** `_record_location(kind, path)` runs when a transfer/verify starts and on app close, for whichever paths are set. If the path is under a currently-known drive mountpoint, `label` and `rel` (path relative to the mountpoint) are stored; otherwise only `path` (fixed folders like `~/Pictures` still restore by path).
- **Restoring.** On startup, after one synchronous `list_usb_drives()` call:
  1. If the saved `path` exists → use it.
  2. Else if `label` matches a mounted drive → use `mountpoint / rel` if that exists, else the mountpoint itself.
  3. Else leave the field empty and show a hint in the card (reusing the muted-hint label style): `Last used drive 'SANDISK' not connected.` The hint clears when the user sets a path or the drive appears (checked during the 3 s poll — if a newly-appeared drive matches a pending remembered label, the path is auto-filled and the hint replaced with a success-colored `✓ SANDISK reconnected`).
- Save/load functions extend `load_profiles_from_disk` / `save_profiles_to_disk` signatures to carry `last_locations` alongside `last_used`, with the same defensive parsing (bad/missing data → ignored).
- Profile application is unchanged: applying a profile still doesn't overwrite paths by default; location memory is a separate mechanism from profiles.

## 4. USB data safety (fsync + cache-bypassing verify)

New option: **"Sync writes to device (recommended for USB)"** — a checkbox in the Options card's logging row area (row 3), default **on**, added to `PROFILE_FIELDS` and all built-in profiles so it persists like the other toggles.

New helper `flush_to_disk(path: Path, drop_cache: bool) -> Optional[str]`:

- Opens the file `O_RDONLY`, calls `os.fsync(fd)`; if `drop_cache` and `os.posix_fadvise` exists, calls `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`; closes the fd.
- Returns an error string on `OSError`, else `None`. On platforms without `posix_fadvise` the cache-drop is silently skipped.

Integration in `_transfer_worker`, immediately after each `shutil.copy2` (both the copy branch and the copy-first-verify-then-delete move branch), when the sync toggle is on:

- `drop_cache=True` only when the effective verify mode is `hash` (dropping cache otherwise is pointless).
- If `flush_to_disk` returns an error: count it as a verify failure — log `✗ sync failed`, remove the bad destination copy, and in move mode keep the source. This mirrors the existing verify-failure handling exactly.
- The plain `shutil.move` fast path (move with verify off) also gets a post-move fsync of the target when the toggle is on; an fsync error there is logged as a WARN (the file has already been moved — deleting it would lose data).

Consequence worth noting in the log: with sync on, the reported MB/s is the true device throughput rather than a cache-inflated figure. A one-line INFO at run start states `sync writes: on` alongside the existing settings line.

`_verify_worker` (verify-only mode) is read-only and unchanged, except that hash verification of the destination benefits from the cache-drop only when the file was just copied in this session — for standalone verify runs, files weren't just written by us, so page-cache contents already came from the device. No change needed there.

## 5. Speed tuning

- `sha256_of` chunk size: 1 MiB → 4 MiB (fewer syscalls on large video files; verify is the hot path in transfer+verify runs).
- Remove redundant `stat` calls in `_transfer_worker`: the source size is fetched once per file and reused (currently `verify_copy` re-stats; it keeps its own stats since it's also used standalone, but the loop's extra `src.exists()`/`stat` duplication is eliminated).
- File copying stays on `shutil.copy2` — CPython on Linux already uses `sendfile` with large blocks, so a custom copy loop would only add risk.

## Error handling summary

| Failure | Behavior |
| --- | --- |
| `lsblk` missing / times out / bad JSON | Fall back to scanning `/run/media/<user>`; combobox still works |
| No drives found | Combobox disabled with placeholder text; Browse/typing unaffected |
| `profiles.json` has bad/missing `last_locations` | Ignored; app starts with empty paths as today |
| Remembered drive not connected | Empty field + muted hint; auto-fills if drive appears later |
| `fsync` fails after copy | Treated as verify failure: bad copy removed, source kept, error logged |
| `fsync` fails after plain move | WARN logged, file kept (already moved) |
| `posix_fadvise` unavailable | Cache-drop skipped silently |

## Testing

The project has no test infrastructure (single script, no git). Verification approach:

- The new pure functions (`list_usb_drives`'s JSON-parsing core, location record/resolve logic, `flush_to_disk`) are written so the parsing/resolution logic takes plain data (parsed JSON dict, drive list) and can be exercised with `python -c` snippets against captured `lsblk -J` output.
- Manual end-to-end check: launch the app, plug/unplug a USB stick and watch the combobox update; run a small copy with sync on and confirm the log; restart the app and confirm paths restore; unplug the remembered drive, restart, confirm the hint, replug, confirm auto-fill.
- `python -m py_compile Organizer_Linux.py` after each change set.
