"""Unit tests for the pure helper functions in File_Organizer.py.

Run from the project root:  python -m unittest tests.test_organizer -v
Everything runs headless except GoogleDriveGuiTests, which is skipped when no
display is available; it drives the real Tk app against a fake rclone binary.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import File_Organizer as org


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
                 "tran": None, "type": "part", "fstype": "exfat"},
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
        {   # USB disk with unmounted partitions — listed as mountable,
            # except for the swap partition which can't hold photos.
            "name": "sdb", "label": None, "mountpoint": None,
            "size": 1000204886016, "fsavail": None, "rm": False,
            "tran": "usb", "type": "disk",
            "children": [
                {"name": "sdb1", "label": "ARCHIVE", "mountpoint": None,
                 "size": 1000203837440, "fsavail": None, "rm": False,
                 "tran": None, "type": "part", "fstype": "exfat"},
                {"name": "sdb2", "label": None, "mountpoint": None,
                 "size": 1048576, "fsavail": None, "rm": False,
                 "tran": None, "type": "part", "fstype": "swap"},
                {"name": "sdb3", "label": None, "mountpoint": None,
                 "size": 1048576, "fsavail": None, "rm": False,
                 "tran": None, "type": "part", "fstype": None},
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

    def test_excludes_system_mounts(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        mountpoints = {d.mountpoint for d in drives}
        self.assertNotIn("/", mountpoints)
        self.assertNotIn("/boot", mountpoints)
        self.assertEqual(len(drives), 3)  # SANDISK + SD card + ARCHIVE

    def test_includes_unmounted_usb_partition_with_filesystem(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        archive = [d for d in drives if d.device == "sdb1"]
        self.assertEqual(len(archive), 1)
        d = archive[0]
        self.assertEqual(d.label, "ARCHIVE")
        self.assertEqual(d.mountpoint, "")
        self.assertFalse(d.mounted)
        self.assertEqual(d.free, 0)

    def test_mounted_drives_report_mounted(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        sandisk = next(d for d in drives if d.device == "sda1")
        self.assertTrue(sandisk.mounted)

    def test_captures_filesystem_type(self):
        drives = org.parse_lsblk_drives(LSBLK_FIXTURE)
        by_dev = {d.device: d for d in drives}
        self.assertEqual(by_dev["sda1"].fstype, "exfat")
        self.assertEqual(by_dev["sdb1"].fstype, "exfat")
        # no fstype column (older lsblk / fallback scan): empty string
        self.assertEqual(by_dev["mmcblk0p1"].fstype, "")

    def test_skips_unmounted_partitions_without_usable_filesystem(self):
        devices = {d.device for d in org.parse_lsblk_drives(LSBLK_FIXTURE)}
        self.assertNotIn("sdb2", devices)  # swap
        self.assertNotIn("sdb3", devices)  # no filesystem

    def test_empty_or_malformed_input_returns_empty_list(self):
        self.assertEqual(org.parse_lsblk_drives({}), [])
        self.assertEqual(org.parse_lsblk_drives({"blockdevices": None}), [])


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
        self.assertEqual({d.label for d in drives},
                         {"SANDISK", "9016-4EF8", "ARCHIVE"})

    def test_never_raises_on_garbage_output(self):
        proc = mock.Mock(returncode=0, stdout="not json")
        with mock.patch.object(org.subprocess, "run", return_value=proc), \
             mock.patch.object(org, "_scan_media_dirs", return_value=[]):
            self.assertEqual(org.list_usb_drives(), [])


class MountUnmountTests(unittest.TestCase):
    def test_mount_drive_parses_mountpoint(self):
        proc = mock.Mock(returncode=0, stderr="",
                         stdout="Mounted /dev/sdb1 at /run/media/nikits/ARCHIVE\n")
        with mock.patch.object(org.subprocess, "run",
                               return_value=proc) as run:
            ok, detail = org.mount_drive("sdb1")
        self.assertTrue(ok)
        self.assertEqual(detail, "/run/media/nikits/ARCHIVE")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["udisksctl", "mount", "-b", "/dev/sdb1"])

    def test_mount_drive_parses_older_udisks_trailing_period(self):
        proc = mock.Mock(returncode=0, stderr="",
                         stdout="Mounted /dev/sdb1 at /run/media/nikits/ARCHIVE.\n")
        with mock.patch.object(org.subprocess, "run", return_value=proc):
            ok, detail = org.mount_drive("sdb1")
        self.assertTrue(ok)
        self.assertEqual(detail, "/run/media/nikits/ARCHIVE")

    def test_mount_drive_failure_returns_error_message(self):
        proc = mock.Mock(returncode=1, stdout="",
                         stderr="Error mounting /dev/sdb1: already mounted\n")
        with mock.patch.object(org.subprocess, "run", return_value=proc):
            ok, detail = org.mount_drive("sdb1")
        self.assertFalse(ok)
        self.assertIn("already mounted", detail)

    def test_unmount_drive_success_and_failure(self):
        proc = mock.Mock(returncode=0, stdout="Unmounted /dev/sdb1.\n",
                         stderr="")
        with mock.patch.object(org.subprocess, "run",
                               return_value=proc) as run:
            ok, detail = org.unmount_drive("sdb1")
        self.assertTrue(ok)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["udisksctl", "unmount", "-b", "/dev/sdb1"])

        proc = mock.Mock(returncode=1, stdout="",
                         stderr="target is busy\n")
        with mock.patch.object(org.subprocess, "run", return_value=proc):
            ok, detail = org.unmount_drive("sdb1")
        self.assertFalse(ok)
        self.assertIn("busy", detail)

    def test_mount_drive_survives_missing_udisksctl(self):
        with mock.patch.object(org.subprocess, "run",
                               side_effect=FileNotFoundError("no udisksctl")):
            ok, detail = org.mount_drive("sdb1")
        self.assertFalse(ok)
        self.assertTrue(detail)


class DriveHoldDiagnosisTests(unittest.TestCase):
    def test_finds_own_open_file_under_mountpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / "held.jpg", "w"):
                holders = org.processes_using_mount(tmp)
            self.assertTrue(any(f"[{os.getpid()}]" in h for h in holders),
                            f"own pid not found in {holders!r}")
            self.assertTrue(any("this app" in h for h in holders))

    def test_quiet_when_nothing_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "empty-mount"
            sub.mkdir()
            self.assertEqual(org.processes_using_mount(str(sub)), [])

    def test_unmount_retries_once_on_busy(self):
        results = iter([(False, "target is busy"), (True, "Unmounted.")])
        with mock.patch.object(org, "unmount_drive",
                               side_effect=lambda d: next(results)), \
             mock.patch.object(org.time, "sleep"):
            ok, msg = org.unmount_drive_with_diagnosis("sdb2", "/mnt/x")
        self.assertTrue(ok)

    def test_unmount_reports_holders_when_still_busy(self):
        with mock.patch.object(org, "unmount_drive",
                               return_value=(False, "target is busy")), \
             mock.patch.object(org, "processes_using_mount",
                               return_value=["nautilus[123]: open file /mnt/x/a"]), \
             mock.patch.object(org.time, "sleep"):
            ok, msg = org.unmount_drive_with_diagnosis("sdb2", "/mnt/x")
        self.assertFalse(ok)
        self.assertIn("nautilus", msg)

    def test_unmount_non_busy_failure_returns_immediately(self):
        calls = []

        def fake(dev):
            calls.append(dev)
            return False, "no such device"

        with mock.patch.object(org, "unmount_drive", side_effect=fake):
            ok, msg = org.unmount_drive_with_diagnosis("sdb2", "/mnt/x")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)


class FormatDriveTests(unittest.TestCase):
    def test_builds_busctl_udisks_format_call(self):
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(org.subprocess, "run",
                               return_value=proc) as run:
            ok, msg = org.format_drive("sdb1", "exfat", "ARCHIVE")
        self.assertTrue(ok)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "busctl")
        self.assertIn("/org/freedesktop/UDisks2/block_devices/sdb1", cmd)
        self.assertIn("Format", cmd)
        self.assertIn("exfat", cmd)
        self.assertIn("ARCHIVE", cmd)

    def test_falls_back_to_gdbus_when_busctl_missing(self):
        proc = mock.Mock(returncode=0, stdout="()", stderr="")
        calls = []

        def fake_run(cmd, **_kw):
            calls.append(cmd[0])
            if cmd[0] == "busctl":
                raise FileNotFoundError("no busctl")
            return proc

        with mock.patch.object(org.subprocess, "run", side_effect=fake_run):
            ok, msg = org.format_drive("sdb1", "vfat", "")
        self.assertTrue(ok)
        self.assertEqual(calls, ["busctl", "gdbus"])

    def test_refuses_unknown_device_or_fstype(self):
        with mock.patch.object(org.subprocess, "run") as run:
            self.assertFalse(org.format_drive("", "vfat")[0])
            self.assertFalse(org.format_drive("sdb1", "")[0])
        run.assert_not_called()

    def test_failure_returns_daemon_message(self):
        proc = mock.Mock(returncode=1, stdout="",
                         stderr="Error formatting: not authorized\n")
        with mock.patch.object(org.subprocess, "run", return_value=proc):
            ok, msg = org.format_drive("sdb1", "vfat", "X")
        self.assertFalse(ok)
        self.assertIn("not authorized", msg)

    def test_sanitizes_filesystem_label(self):
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(org.subprocess, "run",
                               return_value=proc) as run:
            org.format_drive("sdb1", "vfat", "BAD'LABEL\\{}")
        cmd = run.call_args[0][0]
        self.assertIn("BADLABEL", cmd)
        for arg in cmd:
            self.assertNotIn("'", arg)


class FilesIdenticalTests(unittest.TestCase):
    def test_faithful_copy_is_identical_in_both_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            a.write_bytes(b"x" * 100)
            b = Path(tmp) / "b.jpg"
            shutil.copy2(a, b)
            self.assertTrue(org.files_identical(a, b, "off"))
            self.assertTrue(org.files_identical(a, b, "quick"))
            self.assertTrue(org.files_identical(a, b, "hash"))

    def test_different_size_is_never_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            a.write_bytes(b"x" * 100)
            b = Path(tmp) / "b.jpg"
            b.write_bytes(b"x" * 50)
            shutil.copystat(a, b)
            self.assertFalse(org.files_identical(a, b, "off"))
            self.assertFalse(org.files_identical(a, b, "hash"))

    def test_same_size_different_content_caught_by_hash_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            a.write_bytes(b"x" * 100)
            b = Path(tmp) / "b.jpg"
            b.write_bytes(b"y" * 100)
            shutil.copystat(a, b)
            self.assertFalse(org.files_identical(a, b, "hash"))

    def test_missing_destination_is_not_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            a.write_bytes(b"x")
            self.assertFalse(org.files_identical(a, Path(tmp) / "gone", "off"))


class OmarchyWatcherTests(unittest.TestCase):
    def test_inotify_wait_returns_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            ino = org.Inotify()
            try:
                ino.add_watch(tmp)
                fired = threading.Event()
                threading.Thread(target=lambda: (ino.wait(), fired.set()),
                                 daemon=True).start()
                time.sleep(0.05)
                (Path(tmp) / "colors.toml").write_text("x")
                self.assertTrue(fired.wait(2.0))
            finally:
                ino.close()

    def test_watcher_notifies_on_theme_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            current.mkdir()
            with mock.patch.object(org, "OMARCHY_CURRENT_DIR", current), \
                 mock.patch.object(org, "OMARCHY_COLORS_PATH",
                                   current / "theme" / "colors.toml"):
                hits = threading.Event()
                self.assertTrue(org.start_omarchy_watcher(hits.set))
                time.sleep(0.05)
                (current / "theme.name").write_text("catppuccin")
                self.assertTrue(hits.wait(3.0))

    def test_watcher_reports_failure_without_omarchy_dir(self):
        with mock.patch.object(org, "OMARCHY_CURRENT_DIR",
                               Path("/nonexistent-omarchy-xyz")):
            self.assertFalse(org.start_omarchy_watcher(lambda: None))


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

    def test_record_ignores_unmounted_drives(self):
        # An unmounted drive has mountpoint "" — it must never match, or
        # Path("").resolve() would equate it with the current directory.
        unmounted = [org.UsbDrive("ARCHIVE", "", 10, 0, "sdb1")]
        rec = org.make_location_record(os.getcwd(), unmounted)
        self.assertNotIn("label", rec)

    def test_resolve_treats_unmounted_drive_as_missing(self):
        unmounted = [org.UsbDrive("ARCHIVE", "", 10, 0, "sdb1")]
        rec = {"path": "/gone", "label": "ARCHIVE", "rel": "."}
        path, missing = org.resolve_location(rec, unmounted)
        self.assertIsNone(path)
        self.assertEqual(missing, "ARCHIVE")

    def test_resolve_tolerates_junk(self):
        self.assertEqual(org.resolve_location(None, []), (None, None))
        self.assertEqual(org.resolve_location("bogus", []), (None, None))
        self.assertEqual(org.resolve_location({}, []), (None, None))

    def test_symlink_mountpoint_resolution(self):
        """Verify that make_location_record resolves symlinked mountpoints.

        Finding 1 issue: if a mountpoint is a symlink, the unresolved
        mountpoint in the relative_to comparison would fail. Both the
        membership test and the final rel computation must resolve the
        mountpoint.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Create real mount directory
            real_mount = Path(tmp) / "real_mount"
            real_mount.mkdir()
            (real_mount / "DCIM").mkdir()

            # Create symlink to the mount
            link_mount = Path(tmp) / "link_mount"
            link_mount.symlink_to(real_mount)

            # Register the drive using the symlink as mountpoint
            drives = [org.UsbDrive("SANDISK", str(link_mount),
                                   64_000_000_000, 12_000_000_000, "sda1")]

            # Try to make a location record using the real directory path
            rec = org.make_location_record(str(real_mount / "DCIM"), drives)

            # Should find the drive and resolve the symlink correctly
            self.assertEqual(rec["label"], "SANDISK")
            self.assertEqual(rec["rel"], "DCIM")


class PersistenceTests(unittest.TestCase):
    def _patch_paths(self, tmp):
        pdir = Path(tmp) / ".file_organizer"
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


class ParseSimpleTomlTests(unittest.TestCase):
    def test_parses_quoted_key_value_lines(self):
        d = org._parse_simple_toml('accent = "#7fbbb3"\nfoo = "bar"\n')
        self.assertEqual(d, {"accent": "#7fbbb3", "foo": "bar"})

    def test_ignores_comments_sections_and_unquoted_values(self):
        text = '# comment\n\n[section]\nnum = 5\nok = "1"\n'
        self.assertEqual(org._parse_simple_toml(text), {"ok": "1"})

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(org._parse_simple_toml(""), {})


class ColorMathTests(unittest.TestCase):
    def test_mix_endpoints_and_midpoint(self):
        self.assertEqual(org._mix("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(org._mix("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(org._mix("#000000", "#ffffff", 0.5), "#808080")

    def test_mix_clamps_t_out_of_range(self):
        self.assertEqual(org._mix("#102030", "#ffffff", -1.0), "#102030")
        self.assertEqual(org._mix("#102030", "#ffffff", 2.0), "#ffffff")

    def test_lighten_and_darken_are_mixes_toward_white_and_black(self):
        self.assertEqual(org._lighten("#000000", 1.0), "#ffffff")
        self.assertEqual(org._darken("#ffffff", 1.0), "#000000")
        self.assertEqual(org._lighten("#2d353b", 0.0), "#2d353b")

    def test_invalid_hex_raises_valueerror(self):
        with self.assertRaises(ValueError):
            org._mix("nope", "#ffffff", 0.5)
        with self.assertRaises(ValueError):
            org._mix("#ffffff", "#ffff", 0.5)


EVERFOREST_COLORS_TOML = '''\
accent = "#7fbbb3"
cursor = "#d3c6aa"
foreground = "#d3c6aa"
background = "#2d353b"
color0 = "#475258"
color1 = "#e67e80"
color2 = "#a7c080"
color3 = "#dbbc7f"
color4 = "#7fbbb3"
'''


class LoadOmarchyPaletteTests(unittest.TestCase):
    def _palette_from(self, toml_text):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "colors.toml"
            p.write_text(toml_text, encoding="utf-8")
            return org.load_omarchy_palette(p)

    def test_full_theme_produces_complete_palette(self):
        pal = self._palette_from(EVERFOREST_COLORS_TOML)
        self.assertEqual(set(pal), set(org.THEMES["dark"]))
        self.assertEqual(pal["bg"], "#2d353b")
        self.assertEqual(pal["text"], "#d3c6aa")
        self.assertEqual(pal["accent"], "#7fbbb3")
        self.assertEqual(pal["accent_fg"], "#2d353b")
        self.assertEqual(pal["success"], "#a7c080")
        self.assertEqual(pal["warning"], "#dbbc7f")
        self.assertEqual(pal["error"], "#e67e80")

    def test_missing_file_returns_none(self):
        missing = Path(tempfile.gettempdir()) / "no-such-dir-xyz" / "colors.toml"
        self.assertIsNone(org.load_omarchy_palette(missing))

    def test_missing_background_or_foreground_returns_none(self):
        self.assertIsNone(self._palette_from('foreground = "#d3c6aa"\n'))
        self.assertIsNone(self._palette_from('background = "#2d353b"\n'))

    def test_accent_falls_back_to_color4_then_foreground(self):
        base = 'foreground = "#d3c6aa"\nbackground = "#2d353b"\n'
        pal = self._palette_from(base + 'color4 = "#7fbbb3"\n')
        self.assertEqual(pal["accent"], "#7fbbb3")
        pal = self._palette_from(base)
        self.assertEqual(pal["accent"], "#d3c6aa")

    def test_non_hex_values_are_ignored(self):
        pal = self._palette_from(
            'foreground = "#d3c6aa"\nbackground = "#2d353b"\naccent = "red"\n')
        self.assertEqual(pal["accent"], "#d3c6aa")  # bad accent ignored -> fg


class OmarchyFingerprintTests(unittest.TestCase):
    def test_existing_file_yields_path_and_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "colors.toml"
            p.write_text("x", encoding="utf-8")
            fp = org.omarchy_theme_fingerprint(p)
            self.assertEqual(fp[0], str(p.resolve()))
            self.assertEqual(fp[1], p.stat().st_mtime)

    def test_missing_file_returns_none(self):
        missing = Path(tempfile.gettempdir()) / "no-such-dir-xyz" / "colors.toml"
        self.assertIsNone(org.omarchy_theme_fingerprint(missing))


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

    def test_skips_dependency_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            for junk in ("node_modules", "venv", "__pycache__"):
                d = src / junk
                d.mkdir()
                (d / "junk.js").write_bytes(b"x")
            (src / "real.txt").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, True, cancel)]
            self.assertEqual(found, ["real.txt"])

    def test_skips_os_metadata_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            for meta in ("Thumbs.db", "desktop.ini", "ehthumbs.db"):
                (src / meta).write_bytes(b"x")
            (src / "real.txt").write_bytes(b"y")
            cancel = threading.Event()
            found = [p.name for p in org.walk_files(src, True, cancel)]
            self.assertEqual(found, ["real.txt"])
            found = [p.name for p in org.walk_files(src, False, cancel)]
            self.assertEqual(found, ["real.txt"])


class LegacyProfileMigrationTests(unittest.TestCase):
    """The rebrand moved PROFILES_DIR; old profiles must not be abandoned."""

    def _patch_paths(self, tmp):
        pdir = Path(tmp) / ".file_organizer"
        pfile = pdir / "profiles.json"
        return pdir, pfile, mock.patch.multiple(
            org, PROFILES_DIR=pdir, PROFILES_FILE=pfile)

    def _write_legacy(self, tmp, payload):
        legacy = Path(tmp) / ".photo_organizer"
        legacy.mkdir()
        (legacy / "profiles.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def test_migrates_profiles_and_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_legacy(tmp, {
                "profiles": {"Mine": {"operation": "move", "theme": "dark"}},
                "last_used": "Mine",
                "last_locations": {"source": {"path": "/src"},
                                   "dest": {"path": "/dst"}},
            })
            pdir, pfile, patcher = self._patch_paths(tmp)
            with patcher:
                profiles, last_used, locations = org.load_profiles_from_disk()
            self.assertEqual(profiles["Mine"]["operation"], "move")
            self.assertEqual(last_used, "Mine")
            self.assertEqual(locations["dest"]["path"], "/dst")
            self.assertTrue(pfile.is_file())

    def test_does_not_overwrite_existing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_legacy(tmp, {"profiles": {"Legacy": {"theme": "light"}}})
            pdir, pfile, patcher = self._patch_paths(tmp)
            pdir.mkdir()
            pfile.write_text(json.dumps({"profiles": {"New": {"theme": "dark"}}}),
                             encoding="utf-8")
            with patcher:
                profiles, _, _ = org.load_profiles_from_disk()
            self.assertIn("New", profiles)
            self.assertNotIn("Legacy", profiles)

    def test_corrupt_legacy_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / ".photo_organizer"
            legacy.mkdir()
            (legacy / "profiles.json").write_text("{not json", encoding="utf-8")
            pdir, pfile, patcher = self._patch_paths(tmp)
            with patcher:
                profiles, _, _ = org.load_profiles_from_disk()
            self.assertFalse(pfile.exists())
            self.assertIn("Default — Copy by Year/Month", profiles)

    def test_migration_runs_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_legacy(tmp, {"profiles": {"Mine": {"theme": "dark"}}})
            pdir, pfile, patcher = self._patch_paths(tmp)
            with patcher:
                self.assertTrue(org.migrate_legacy_profiles())
                self.assertFalse(org.migrate_legacy_profiles())


class DetectStructureTests(unittest.TestCase):
    def test_detects_non_media_type_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "PDF").mkdir()
            (dest / "ZIP").mkdir()
            self.assertEqual(org.detect_folder_structure(dest), "file_type")


class GetPhotoDateTests(unittest.TestCase):
    def test_non_media_file_uses_filesystem_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "notes.txt"
            f.write_bytes(b"hi")
            os.utime(f, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
            self.assertAlmostEqual(
                org.get_photo_date(f).timestamp(), 1_700_000_000.0)


class PackagingTests(unittest.TestCase):
    TEMPLATE = PROJECT_ROOT / "packaging" / "file-organizer.desktop"

    def test_template_has_required_keys(self):
        text = self.TEMPLATE.read_text()
        for key in ("[Desktop Entry]", "Type=Application",
                    "Name=File Organizer",
                    "Comment=Copy or move files of any type, sorted by date or type",
                    "Exec=python3 @PROJECT_DIR@/File_Organizer.py",
                    "Icon=file-organizer", "Terminal=false",
                    "Categories=Utility;FileTools;",
                    "StartupWMClass=FileOrganizer"):
            self.assertIn(key, text)

    def test_template_has_project_dir_placeholder(self):
        self.assertIn("@PROJECT_DIR@",
                      self.TEMPLATE.read_text())

    def test_startup_wm_class_matches_app_classname(self):
        template = self.TEMPLATE.read_text()
        wm = re.search(r"StartupWMClass=(\S+)", template).group(1)
        source = (PROJECT_ROOT / "File_Organizer.py").read_text()
        self.assertIn(f'className="{wm}"', source)


class SvgIconTests(unittest.TestCase):
    ICON = PROJECT_ROOT / "packaging" / "file-organizer.svg"

    def test_icon_is_well_formed_svg(self):
        root = ET.parse(str(self.ICON)).getroot()
        self.assertEqual(root.tag.split("}")[-1], "svg")

    def test_icon_has_square_geometry(self):
        root = ET.parse(str(self.ICON)).getroot()
        parts = (root.get("viewBox") or "").replace(",", " ").split()
        self.assertEqual(parts, ["0", "0", "256", "256"])

    def test_icon_uses_omarchy_palette(self):
        text = self.ICON.read_text()
        for color in ("#7daea3", "#282828", "#d4be98"):
            self.assertIn(color, text)


class InstallerTests(unittest.TestCase):
    INSTALLER = PROJECT_ROOT / "packaging" / "install.sh"

    def _run(self, tmp: str, *args: str):
        env = dict(os.environ)
        env["HOME"] = tmp
        return subprocess.run(
            [str(self.INSTALLER), *args], cwd=tmp, env=env,
            capture_output=True, text=True)

    def test_installs_desktop_and_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            desktop = (Path(tmp) / ".local/share/applications"
                       / "file-organizer.desktop")
            icon = (Path(tmp) / ".local/share/icons/hicolor/scalable/apps"
                    / "file-organizer.svg")
            self.assertTrue(desktop.is_file())
            self.assertTrue(icon.is_file())
            self.assertIn(str(PROJECT_ROOT), desktop.read_text())

    def test_removes_stale_photo_organizer_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = Path(tmp) / ".local/share/applications"
            apps.mkdir(parents=True)
            stale = apps / "photo-organizer.desktop"
            stale.write_text("[Desktop Entry]\n")
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(stale.exists())

    def test_uninstall_removes_installed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(tmp).returncode, 0)
            proc = self._run(tmp, "--uninstall")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse((Path(tmp) / ".local/share/applications"
                              / "file-organizer.desktop").exists())
            self.assertFalse((Path(tmp) / ".local/share/icons/hicolor"
                              / "scalable/apps/file-organizer.svg").exists())

    def test_reinstall_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = (Path(tmp) / ".local/share/applications"
                       / "file-organizer.desktop")
            icon = (Path(tmp) / ".local/share/icons/hicolor/scalable/apps"
                    / "file-organizer.svg")
            self.assertEqual(self._run(tmp).returncode, 0)
            first = desktop.read_text()
            self.assertEqual(self._run(tmp).returncode, 0)
            self.assertEqual(desktop.read_text(), first)
            self.assertTrue(desktop.is_file())
            self.assertTrue(icon.is_file())


# A stand-in for the real rclone binary. It implements just the subcommands the
# app uses, backed by a directory, so Drive paths, hashes and transfers can be
# tested end-to-end without network access or credentials.
FAKE_RCLONE_SOURCE = r'''#!/usr/bin/env python3
import hashlib, json, os, shutil, sys, time
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "testremote"
ROOT = Path(os.environ.get("STUB_ROOT", "."))


def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def record(p, base):
    st = p.stat()
    return {"Path": p.relative_to(base).as_posix(), "Name": p.name,
            "Size": st.st_size, "IsDir": False,
            "ModTime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "Hashes": {"md5": md5_of(p)}}


def to_local(spec):
    name, _, rest = spec.partition(":")
    if name != REMOTE:
        sys.stderr.write("unknown remote %s\n" % name)
        sys.exit(2)
    return ROOT / rest.strip("/")


def is_spec(text):
    return text.partition(":")[0] == REMOTE


args = sys.argv[1:]
cmd = args[0] if args else ""
rest = args[1:]
flags = [a for a in rest if a.startswith("-")]
pos = [a for a in rest if not a.startswith("-")]

if cmd == "listremotes":
    print(REMOTE + ":")
elif cmd == "lsjson":
    target = to_local(pos[0])
    if "--stat" in flags:
        if not target.is_file():
            sys.stderr.write("object not found\n")
            sys.exit(3)
        print(json.dumps(record(target, target.parent)))
    else:
        if not target.is_dir():
            print("[]")
            sys.exit(0)
        items = list(target.rglob("*")) if "-R" in flags else list(target.iterdir())
        print(json.dumps([record(p, target) for p in sorted(items) if p.is_file()]))
elif cmd == "copyto":
    src, dst = pos[0], pos[1]
    if os.environ.get("STUB_SLEEP"):
        time.sleep(float(os.environ["STUB_SLEEP"]))
    if is_spec(src):
        src_path, dst_path = to_local(src), Path(dst)
    else:
        src_path, dst_path = Path(src), to_local(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_path, dst_path)
elif cmd == "deletefile":
    to_local(pos[0]).unlink()
elif cmd == "about":
    print(json.dumps({"total": 1000, "used": 250, "free": 750, "trashed": 0}))
else:
    sys.stderr.write("unsupported: %s\n" % cmd)
    sys.exit(2)
'''


class RcloneBackendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.remote_root = root / "remote"
        self.remote_root.mkdir()
        binary = root / "bin" / "rclone"
        binary.parent.mkdir()
        binary.write_text(FAKE_RCLONE_SOURCE)
        binary.chmod(0o755)
        self.bin = binary
        self._env = mock.patch.dict(os.environ, {
            org.RCLONE_ENV_VAR: str(binary),
            "STUB_ROOT": str(self.remote_root),
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def put_remote(self, rel, content):
        p = self.remote_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def local_file(self, name="a.txt", content=b"hello"):
        p = self.root / "local" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_list_remotes(self):
        self.assertEqual(org.list_remotes(), ["testremote"])

    def test_split_and_join(self):
        self.assertEqual(org.split_remote_spec("gdrive:Photos/2024"),
                         ("gdrive", "Photos/2024"))
        self.assertIsNone(org.split_remote_spec("/home/user/photos"))
        self.assertEqual(org.join_remote("testremote:Photos", "2024", "a.jpg"),
                         "testremote:Photos/2024/a.jpg")
        self.assertEqual(org.join_remote("testremote:"), "testremote:")

    def test_join_rejects_traversal(self):
        with self.assertRaises(ValueError):
            org.join_remote("testremote:Photos", "..", "etc")
        with self.assertRaises(ValueError):
            org.join_remote("not-a-spec")

    def test_is_remote_path_requires_configured_remote(self):
        self.assertTrue(org.is_remote_path("testremote:Photos"))
        self.assertFalse(org.is_remote_path("elsewhere:Photos"))
        self.assertFalse(org.is_remote_path("/home/user/Photos"))
        self.assertFalse(org.is_remote_path("notes: 2024"))

    def test_scan_remote_recursive(self):
        self.put_remote("Photos/2024/a.jpg", b"aaa")
        self.put_remote("Photos/2024/sub/b.jpg", b"bbbb")
        entries = org.scan_remote("testremote:Photos")
        self.assertEqual(sorted(e.name for e in entries), ["a.jpg", "b.jpg"])
        self.assertEqual({e.name: e.size for e in entries},
                         {"a.jpg": 3, "b.jpg": 4})
        self.assertTrue(all(e.md5 for e in entries))
        self.assertTrue(all(e.modtime is not None for e in entries))

    def test_scan_remote_flat_lists_direct_children(self):
        self.put_remote("Photos/top.jpg", b"x")
        self.put_remote("Photos/sub/deep.jpg", b"yy")
        entries = org.scan_remote("testremote:Photos", recursive=False)
        self.assertEqual([e.name for e in entries], ["top.jpg"])

    def test_remote_stat_present_and_absent(self):
        self.put_remote("Photos/a.jpg", b"abc")
        entry = org.remote_stat("testremote:Photos/a.jpg")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.size, 3)
        self.assertIsNone(org.remote_stat("testremote:Photos/missing.jpg"))

    def test_upload_then_verify(self):
        local = self.local_file(content=b"payload-123")
        spec = "testremote:Photos/2024/a.txt"
        ok, err = org.upload_file(local, spec)
        self.assertTrue(ok, err)
        self.assertTrue((self.remote_root / "Photos/2024/a.txt").is_file())
        self.assertEqual(org.remote_verify(local, spec, "quick"), (True, ""))
        self.assertEqual(org.remote_verify(local, spec, "hash"), (True, ""))
        self.assertEqual(org.remote_verify(local, spec, "off"), (True, ""))

    def test_verify_detects_size_then_hash_mismatch(self):
        local = self.local_file(content=b"abcdef")
        spec = "testremote:Photos/a.txt"
        ok, err = org.upload_file(local, spec)
        self.assertTrue(ok, err)

        # same length, different bytes -> size passes, MD5 must fail
        self.put_remote("Photos/a.txt", b"ABCDEF")
        ok_quick, _ = org.remote_verify(local, spec, "quick")
        ok_hash, why = org.remote_verify(local, spec, "hash")
        self.assertTrue(ok_quick)
        self.assertFalse(ok_hash)
        self.assertIn("MD5", why)

        # different length -> even quick fails
        self.put_remote("Photos/a.txt", b"short")
        ok_quick, why = org.remote_verify(local, spec, "quick")
        self.assertFalse(ok_quick)
        self.assertIn("size", why)

    def test_missing_remote_file_fails_verify(self):
        local = self.local_file()
        self.assertEqual(org.remote_verify(local, "testremote:nope.txt", "quick"),
                         (False, "not found on remote"))

    def test_download_and_delete(self):
        self.put_remote("Photos/b.bin", b"binary-bytes")
        target = self.root / "out" / "b.bin"
        ok, err = org.download_file("testremote:Photos/b.bin", target)
        self.assertTrue(ok, err)
        self.assertEqual(target.read_bytes(), b"binary-bytes")
        ok, err = org.delete_remote_file("testremote:Photos/b.bin")
        self.assertTrue(ok, err)
        self.assertFalse((self.remote_root / "Photos/b.bin").exists())

    def test_remote_quota(self):
        quota = org.remote_quota("testremote")
        self.assertEqual(quota["free"], 750)
        self.assertEqual(quota["total"], 1000)

    def test_cancel_terminates_a_running_transfer(self):
        local = self.local_file(content=b"x" * 1024)
        cancel = threading.Event()
        cancel.set()
        started = time.time()
        with mock.patch.dict(os.environ, {"STUB_SLEEP": "10"}):
            ok, err = org.upload_file(local, "testremote:Photos/slow.bin",
                                      cancel_event=cancel)
        self.assertFalse(ok)
        self.assertIn("cancelled", err)
        self.assertLess(time.time() - started, 5.0, "cancel must not wait for rclone")
        self.assertFalse((self.remote_root / "Photos/slow.bin").exists())

    def test_missing_rclone_is_graceful(self):
        with mock.patch.dict(os.environ,
                             {org.RCLONE_ENV_VAR: "/nonexistent/rclone"}):
            self.assertIsNone(org.rclone_bin())
            self.assertEqual(org.list_remotes(), [])
            self.assertFalse(org.is_remote_path("testremote:Photos"))
            ok, err = org.upload_file(self.local_file(), "testremote:x")
            self.assertFalse(ok)
            self.assertIn("rclone", err)
            self.assertIsNone(org.remote_quota("testremote"))

class RemotePathTests(unittest.TestCase):
    """Target/sort helpers shared by the local and Drive workers."""

    def test_subfolder_for_date_matches_local_sorting(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.jpg"
            f.write_bytes(b"x")
            stamp = datetime(2024, 1, 24, 9, 0).timestamp()
            os.utime(f, (stamp, stamp))
            when = datetime.fromtimestamp(stamp)
            for kind, _label in org.SORT_MODES:
                if kind in ("none", "file_type"):
                    continue
                self.assertEqual(
                    org.build_target_subfolder(f, ".jpg", kind),
                    org.subfolder_for_date(when, ".jpg", kind), kind)

    def test_type_folder_needs_no_date_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-there.pdf"
            self.assertEqual(
                org.build_target_subfolder(missing, ".pdf", "file_type"), Path("PDF"))
            self.assertEqual(
                org.build_target_subfolder(missing, "", "file_type"), Path("OTHER"))

    def test_pull_target_sorts_by_remote_modtime(self):
        entry = org.RemoteEntry("gdrive:Photos/a.jpg", 3,
                                datetime(2024, 1, 15, 10, 30))
        self.assertEqual(org.pull_target(Path("/dest"), entry, "year_month"),
                         Path("/dest/2024/01-January/a.jpg"))
        self.assertEqual(org.pull_target(Path("/dest"), entry, "year"),
                         Path("/dest/2024/a.jpg"))
        self.assertEqual(org.pull_target(Path("/dest"), entry, "file_type"),
                         Path("/dest/JPG/a.jpg"))

    def test_pull_target_without_modtime_uses_today(self):
        entry = org.RemoteEntry("gdrive:a.jpg", 1, None)
        target = org.pull_target(Path("/dest"), entry, "year")
        self.assertEqual(target,
                         Path("/dest") / str(datetime.now().year) / "a.jpg")

    def test_remote_target(self):
        self.assertEqual(
            org.remote_target("gdrive:FileOrganizer", Path("2024/01-January"),
                              "a.jpg"),
            "gdrive:FileOrganizer/2024/01-January/a.jpg")
        self.assertEqual(
            org.remote_target("gdrive:FileOrganizer", Path("."), "a.jpg"),
            "gdrive:FileOrganizer/a.jpg")

    def test_remote_rename_target_probes_until_free(self):
        taken = {"gdrive:P/a_2.jpg", "gdrive:P/a_3.jpg"}
        self.assertEqual(
            org.remote_rename_target("gdrive:P/a.jpg",
                                     probe=lambda s: s in taken),
            "gdrive:P/a_4.jpg")
        self.assertIsNone(
            org.remote_rename_target("gdrive:P/a.jpg",
                                     probe=lambda s: True, limit=3))

    def test_split_remote_name(self):
        self.assertEqual(org.split_remote_name("gdrive:P/a.jpg"),
                         ("gdrive:P/a", ".jpg"))
        self.assertEqual(org.split_remote_name("gdrive:a.tar.gz"),
                         ("gdrive:a.tar", ".gz"))


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
                     "GUI test needs a display")
class GoogleDriveGuiTests(unittest.TestCase):
    """Push/Pull through the real Tk app against the fake rclone binary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root_dir = Path(self._tmp.name)
        self.root_dir = root_dir
        self.remote_root = root_dir / "remote"
        self.remote_root.mkdir()
        binary = root_dir / "bin" / "rclone"
        binary.parent.mkdir()
        binary.write_text(FAKE_RCLONE_SOURCE)
        binary.chmod(0o755)
        self._env = mock.patch.dict(os.environ, {
            org.RCLONE_ENV_VAR: str(binary),
            "STUB_ROOT": str(self.remote_root),
        })
        self._env.start()
        self.addCleanup(self._env.stop)

        pdir = root_dir / ".file_organizer"
        self._profiles = mock.patch.multiple(
            org, PROFILES_DIR=pdir, PROFILES_FILE=pdir / "profiles.json")
        self._profiles.start()
        self.addCleanup(self._profiles.stop)

        self.dialogs = {}
        for name, value in (("askyesno", True), ("showinfo", None),
                            ("showwarning", None), ("showerror", None)):
            patcher = mock.patch.object(org.messagebox, name, return_value=value)
            self.dialogs[name] = patcher.start()
            self.addCleanup(patcher.stop)

        self.tk_root = org.tk.Tk()
        self.tk_root.withdraw()
        self.app = org.FileOrganizerApp(self.tk_root)
        self.app.sort_mode_label.set(org.SORT_LABEL_BY_KEY["year_month"])
        self.app.include_subdirs.set(True)
        self.app.verbose_log.set(False)
        self.app.log_to_file.set(False)
        self.app.verify_mode.set("quick")

    def tearDown(self):
        # Cancel the app's periodic after() callbacks before tearing the
        # interpreter down, otherwise Tcl complains about dead commands.
        try:
            for job in self.tk_root.tk.call("after", "info"):
                try:
                    self.tk_root.after_cancel(job)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.tk_root.destroy()
        except Exception:
            pass

    def _wait(self, timeout: float = 30.0) -> None:
        """Run the real Tk loop until the worker thread finishes.

        The app's workers read Tk variables, which requires an active
        mainloop exactly like running the application for real.
        """
        root = self.tk_root
        state = {"dead_at": None}
        deadline = time.time() + timeout

        def check():
            alive = self.app.worker is not None and self.app.worker.is_alive()
            if alive:
                state["dead_at"] = None
            elif state["dead_at"] is None:
                state["dead_at"] = time.time()
            if state["dead_at"] and time.time() - state["dead_at"] > 0.3:
                root.quit()
                return
            if time.time() > deadline:
                root.quit()
                return
            root.after(30, check)

        root.after(30, check)
        root.mainloop()
        self.app._poll_queue()      # drain messages left behind by the worker

    def _scan(self) -> None:
        self.tk_root.after(10, self.app._start_scan)
        self._wait()
        self.assertTrue(self.app.scanned_files, "scan produced no files")

    def _run(self) -> None:
        self.tk_root.after(10, self.app._start_run)
        self._wait()

    def _local_source(self, name: str = "src") -> Path:
        src = self.root_dir / name
        src.mkdir()
        f = src / "report.pdf"
        f.write_bytes(b"pdf-bytes")
        stamp = datetime(2024, 1, 15, 12, 0).timestamp()
        os.utime(f, (stamp, stamp))
        return src

    def test_drive_picker_offers_configured_remotes(self):
        values = [str(v) for v in self.app.dst_drive_combo["values"]]
        self.assertTrue(
            any("testremote" in v and "Google Drive" in v for v in values), values)
        index = next(i for i, v in enumerate(values) if "testremote" in v)
        self.app.dst_drive_combo.current(index)
        self.app._pick_drive("dest")
        self.assertEqual(self.app.dest_path.get(), "testremote:")

    def test_verify_push_reports_missing_files(self):
        src = self.root_dir / "srcv"
        src.mkdir()
        stamp = datetime(2024, 1, 15, 12, 0).timestamp()
        for name, payload in (("present.pdf", b"p"), ("missing.pdf", b"m")):
            f = src / name
            f.write_bytes(payload)
            os.utime(f, (stamp, stamp))
        remote = self.remote_root / "V" / "2024" / "01-January"
        remote.mkdir(parents=True)
        (remote / "present.pdf").write_bytes(b"p")

        self.app.source_path.set(str(src))
        self.app.dest_path.set("testremote:V")
        self.app.run_mode.set("verify")
        self._scan()
        self._run()

        infos = " ".join(str(c.args) for c in self.dialogs["showinfo"].call_args_list)
        self.assertIn("missing", infos.lower())
        self.assertTrue((src / "missing.pdf").exists(), "verify is read-only")

    def test_verify_pull_reports_missing_files(self):
        remote = self.remote_root / "R" / "2024" / "01-January"
        remote.mkdir(parents=True)
        stamp = datetime(2024, 1, 15, 12, 0).timestamp()
        for name, payload in (("here.jpg", b"a"), ("gone.jpg", b"b")):
            f = remote / name
            f.write_bytes(payload)
            os.utime(f, (stamp, stamp))

        dest = self.root_dir / "destv"
        local = dest / "2024" / "01-January"
        local.mkdir(parents=True)
        (local / "here.jpg").write_bytes(b"a")

        self.app.source_path.set("testremote:R")
        self.app.dest_path.set(str(dest))
        self.app.run_mode.set("verify")
        self._scan()
        self._run()

        infos = " ".join(str(c.args) for c in self.dialogs["showinfo"].call_args_list)
        self.assertIn("missing", infos.lower())
        self.assertFalse((local / "gone.jpg").exists(), "verify must not download")

    def test_push_copies_sorted_file_to_drive(self):
        src = self._local_source()
        self.app.source_path.set(str(src))
        self.app.dest_path.set("testremote:FileOrganizer")
        self.app.run_mode.set("transfer")
        self.app.operation.set("copy")
        self.app.dup_mode.set("rename")
        self._scan()
        self._run()

        remote = (self.remote_root / "FileOrganizer" / "2024"
                  / "01-January" / "report.pdf")
        self.assertTrue(remote.is_file(),
                        sorted(p.as_posix() for p in self.remote_root.rglob("*")))
        self.assertEqual(remote.read_bytes(), b"pdf-bytes")
        self.assertTrue((src / "report.pdf").is_file(), "copy must keep the source")

    def test_pull_downloads_into_sorted_folders(self):
        remote_dir = self.remote_root / "FileOrganizer" / "2024" / "01-January"
        remote_dir.mkdir(parents=True)
        stamp = datetime(2024, 1, 15, 12, 0).timestamp()
        for name, payload in (("a.jpg", b"jpg"), ("notes.txt", b"txt")):
            p = remote_dir / name
            p.write_bytes(payload)
            os.utime(p, (stamp, stamp))

        dest = self.root_dir / "dest"
        self.app.source_path.set("testremote:FileOrganizer")
        self.app.dest_path.set(str(dest))
        self.app.run_mode.set("transfer")
        self.app.operation.set("copy")
        self.app.dup_mode.set("rename")
        self._scan()
        self.assertIsInstance(self.app.scanned_files[0], org.RemoteEntry)
        self._run()

        expected = dest / "2024" / "01-January"
        self.assertEqual(sorted(p.name for p in expected.iterdir()),
                         ["a.jpg", "notes.txt"])
        self.assertEqual((expected / "a.jpg").read_bytes(), b"jpg")
        self.assertTrue((remote_dir / "a.jpg").is_file(), "copy must keep the remote")

    def test_push_move_deletes_source_after_verification(self):
        src = self._local_source("src2")
        self.app.source_path.set(str(src))
        self.app.dest_path.set("testremote:Verified")
        self.app.run_mode.set("transfer_verify")
        self.app.operation.set("move")
        self.app.dup_mode.set("rename")
        self._scan()
        self._run()

        remote = (self.remote_root / "Verified" / "2024"
                  / "01-January" / "report.pdf")
        self.assertTrue(remote.is_file())
        self.assertFalse((src / "report.pdf").exists(),
                         "move should delete the source after a passing verify")


if __name__ == "__main__":
    unittest.main()
