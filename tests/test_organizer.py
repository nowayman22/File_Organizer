"""Unit tests for the pure helper functions in File_Organizer.py.

Run from the project root:  python -m unittest tests.test_organizer -v
These tests never create Tk widgets, so they run headless.
"""
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
