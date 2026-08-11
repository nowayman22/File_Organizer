# Omarchy App Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make File Organizer launchable "like an app" on Omarchy — a `.desktop` entry indexed by Walker plus a custom SVG icon, shipped in the repo with an `install.sh` that installs/uninstalls both on the current machine.

**Architecture:** Add a `packaging/` directory with three assets — a `.desktop` template (containing an `@PROJECT_DIR@` placeholder), a square SVG icon in the active Omarchy palette, and an idempotent bash installer that substitutes the placeholder, copies the assets into `~/.local/share/applications` and the hicolor icon theme, refreshes caches, and removes the stale `photo-organizer.desktop`. Tests cover the template, the SVG, and the installer behavior (run with a faked `HOME`).

**Tech Stack:** Bash, systemd desktop-entry format, SVG, Python 3.9+ stdlib (`unittest`), `subprocess`.

## Global Constraints

- Python 3.9+ standard library; tests are headless and run from the project root: `python -m unittest tests.test_organizer -v`.
- User-level install only (`~/.local/share/applications`, `~/.local/share/icons/hicolor/scalable/apps`); no sudo.
- The `.desktop` template uses the literal token `@PROJECT_DIR@`; the installer substitutes the resolved absolute project path.
- `StartupWMClass=FileOrganizer` must exactly match `className="FileOrganizer"` in `File_Organizer.py` (`main()`, line 3292).
- Icon is a 256×256 square SVG named `file-organizer.svg` in the Omarchy palette: accent `#7daea3`, background `#282828`, foreground `#d4be98`, folder-back `#3c3836`.
- Installer is idempotent, supports `--uninstall`, removes the stale `~/.local/share/applications/photo-organizer.desktop`, and treats cache refresh as best-effort (guarded by `command -v`; failures non-fatal).
- Work continues on branch `feature/file-organizer` (updates open PR #1).

---

### Task 1: Desktop-entry template

**Files:**
- Create: `packaging/file-organizer.desktop`
- Test: `tests/test_organizer.py` (new `PackagingTests` class + `import re` + `PROJECT_ROOT`)

**Interfaces:**
- Produces: `packaging/file-organizer.desktop` — template with the literal `@PROJECT_DIR@` token in `Exec`, `StartupWMClass=FileOrganizer`.
- Consumes: nothing (self-contained).

- [ ] **Step 1: Add the imports and `PROJECT_ROOT`**

In `tests/test_organizer.py`, change the import block at the top from:

```python
import os
import shutil
import tempfile
```
to:
```python
import os
import re
import shutil
import subprocess
import tempfile
```

Then, directly after the line `import File_Organizer as org`, add:

```python

PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

(`subprocess` is used in Task 3; `re` is used now.)

- [ ] **Step 2: Write the failing tests**

Append this class to `tests/test_organizer.py`, before the `if __name__ == "__main__":` block:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.PackagingTests -v`
Expected: ERROR with `FileNotFoundError` (no `packaging/file-organizer.desktop` yet).

- [ ] **Step 4: Create the template**

Create `packaging/file-organizer.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=File Organizer
Comment=Copy or move files of any type, sorted by date or type
Exec=python3 @PROJECT_DIR@/File_Organizer.py
Icon=file-organizer
Terminal=false
Categories=Utility;FileTools;
StartupWMClass=FileOrganizer
Keywords=files;organize;copy;move;sort;
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_organizer.PackagingTests -v`
Expected: 3 tests PASS.

- [ ] **Step 6: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS (75 + 3 = 78).

- [ ] **Step 7: Commit**

```bash
git add packaging/file-organizer.desktop tests/test_organizer.py
git commit -m "feat: add File Organizer desktop-entry template"
```

---

### Task 2: SVG app icon

**Files:**
- Create: `packaging/file-organizer.svg`
- Test: `tests/test_organizer.py` (new `SvgIconTests` class + `import xml.etree.ElementTree as ET`)

**Interfaces:**
- Produces: `packaging/file-organizer.svg` — 256×256 square SVG, well-formed XML, using Omarchy palette colors.
- Consumes: `PROJECT_ROOT` from Task 1.

- [ ] **Step 1: Add the XML import**

In `tests/test_organizer.py`, add to the import block at the top, after `import tempfile`:

```python
import xml.etree.ElementTree as ET
```

- [ ] **Step 2: Write the failing tests**

Append this class to `tests/test_organizer.py`, before the `if __name__ == "__main__":` block:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.SvgIconTests -v`
Expected: ERROR with `FileNotFoundError` (no `packaging/file-organizer.svg` yet).

- [ ] **Step 4: Create the SVG icon**

Create `packaging/file-organizer.svg`:

```svg
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <!-- folder back (darker) -->
  <path d="M32 96 L96 96 L112 80 L224 80 L224 208 L32 208 Z" fill="#3c3836"/>
  <!-- folder front (accent) -->
  <path d="M32 96 L32 208 L224 208 L224 96 Z" fill="#7daea3"/>
  <!-- document sheet -->
  <g transform="rotate(-6 176 128)">
    <rect x="140" y="80" width="80" height="104" rx="10" fill="#282828" stroke="#3c3836" stroke-width="4"/>
    <line x1="158" y1="112" x2="202" y2="112" stroke="#d4be98" stroke-width="6" stroke-linecap="round"/>
    <line x1="158" y1="132" x2="202" y2="132" stroke="#7daea3" stroke-width="6" stroke-linecap="round"/>
    <line x1="158" y1="152" x2="190" y2="152" stroke="#d4be98" stroke-width="6" stroke-linecap="round"/>
  </g>
</svg>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_organizer.SvgIconTests -v`
Expected: 3 tests PASS.

- [ ] **Step 6: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS (78 + 3 = 81).

- [ ] **Step 7: Commit**

```bash
git add packaging/file-organizer.svg tests/test_organizer.py
git commit -m "feat: add File Organizer SVG app icon (Omarchy palette)"
```

---

### Task 3: Installer script

**Files:**
- Create: `packaging/install.sh`
- Test: `tests/test_organizer.py` (new `InstallerTests` class)

**Interfaces:**
- Consumes: `packaging/file-organizer.desktop`, `packaging/file-organizer.svg` (Tasks 1-2), `PROJECT_ROOT` (Task 1).
- Produces: `packaging/install.sh` — run with no args installs; with `--uninstall` removes. Exit 0 on success. Idempotent, best-effort cache refresh, removes the stale `photo-organizer.desktop`.

- [ ] **Step 1: Write the failing tests**

Append this class to `tests/test_organizer.py`, before the `if __name__ == "__main__":` block:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.InstallerTests -v`
Expected: ERROR with `FileNotFoundError` (no `packaging/install.sh` yet) or `PermissionError` (not executable).

- [ ] **Step 3: Create the installer and make it executable**

Create `packaging/install.sh`:

```bash
#!/usr/bin/env bash
# Install / uninstall File Organizer as a desktop app (Omarchy / Walker).
# Usage: install.sh [--uninstall]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

DESKTOP_DEST="$APPS_DIR/file-organizer.desktop"
ICON_DEST="$ICON_DIR/file-organizer.svg"
STALE_ENTRY="$APPS_DIR/photo-organizer.desktop"

refresh_caches() {
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
}

install() {
    mkdir -p "$APPS_DIR" "$ICON_DIR"
    sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
        "$SCRIPT_DIR/file-organizer.desktop" > "$DESKTOP_DEST"
    cp "$SCRIPT_DIR/file-organizer.svg" "$ICON_DEST"
    rm -f "$STALE_ENTRY"
    refresh_caches
    echo "Installed File Organizer launcher."
    echo "  desktop : $DESKTOP_DEST"
    echo "  icon    : $ICON_DEST"
    echo "Open Walker (Super+Space) and search 'File Organizer'."
}

uninstall() {
    rm -f "$DESKTOP_DEST" "$ICON_DEST"
    refresh_caches
    echo "File Organizer launcher removed."
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
else
    install
fi
```

Then make it executable:

```bash
chmod +x packaging/install.sh
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_organizer.InstallerTests -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS (81 + 3 = 84).

- [ ] **Step 6: Commit**

```bash
git add packaging/install.sh tests/test_organizer.py
git commit -m "feat: add File Organizer launcher installer (install/uninstall)"
```

---

### Task 4: Install on this machine and verify

**Files:** none changed in the repo (verification only).

**Interfaces:**
- Consumes: `packaging/install.sh` (Task 3).

- [ ] **Step 1: Run the installer for real**

Run: `packaging/install.sh`
Expected: prints `Installed File Organizer launcher.` plus the two paths. Exit 0.

- [ ] **Step 2: Validate the installed desktop entry**

Run: `desktop-file-validate ~/.local/share/applications/file-organizer.desktop`
Expected: no output (valid).

- [ ] **Step 3: Confirm the stale entry is gone and the icon is installed**

Run: `ls ~/.local/share/applications/photo-organizer.desktop 2>/dev/null && echo "STALE PRESENT" || echo "STALE REMOVED"`
Expected: `STALE REMOVED`.

Run: `ls -l ~/.local/share/icons/hicolor/scalable/apps/file-organizer.svg`
Expected: the file exists.

- [ ] **Step 4: Launch the app (best-effort)**

Run: `gio launch ~/.local/share/applications/file-organizer.desktop`
Expected: the File Organizer window opens. If the session has no display (`$DISPLAY`/`$WAYLAND_DISPLAY` unset), this fails with a connection error — report that and rely on the user's manual check in Step 5. If it launches, close the window afterwards.

- [ ] **Step 5: Confirm Walker indexes it**

Run: `grep -rl "File Organizer" ~/.local/share/applications/ | grep file-organizer.desktop`
Expected: prints the path (Walker re-scans desktop dirs on each open; the entry will appear under "File Organizer").

- [ ] **Step 6: Confirm the repo tree is clean and update the ledger**

Run: `git status --short`
Expected: nothing to commit (installer writes only under `$HOME`, outside the repo).

---

## Self-Review Notes

- **Spec coverage:** §files (template/icon/installer) → Tasks 1-3. §Git workflow → continued on `feature/file-organizer`. §Verification → Task 4. §Testing → Tasks 1-3 test classes. No gaps.
- **No placeholders:** every step shows exact file content and exact commands with expected output.
- **Type consistency:** `PROJECT_ROOT` defined in Task 1 and consumed by Tasks 1-3; `packaging/file-organizer.desktop` and `packaging/file-organizer.svg` created in Tasks 1-2 and consumed by Task 3's installer/tests; `StartupWMClass=FileOrganizer` matches `className="FileOrganizer"` (asserted by Task 1's test). Icon colors asserted in Task 2 match the spec palette.
