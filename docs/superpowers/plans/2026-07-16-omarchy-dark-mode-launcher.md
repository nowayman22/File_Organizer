# Omarchy Dark Mode + Desktop Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app's dark theme derives its colors from the active Omarchy theme (live-updating while running), and the app is launchable from the desktop via a `.desktop` entry.

**Architecture:** All app code stays in the single file `Organizer_Linux.py`. New module-level helpers (TOML-ish parser, hex color math, palette builder, theme fingerprint) live next to the `THEMES` dict. `main()` swaps `THEMES["dark"]` for the Omarchy palette at startup; a `root.after` poll re-checks every 2 s. The launcher is a one-time `.desktop` file in `~/.local/share/applications/`.

**Tech Stack:** Python 3.9+ standard library only, tkinter, unittest.

**Spec:** `docs/superpowers/specs/2026-07-16-omarchy-dark-mode-launcher-design.md`

## Global Constraints

- Python 3.9+ compatible; standard library only (no `tomllib` — it's 3.11+).
- All app code in `Organizer_Linux.py` (single-file app).
- Tests are headless (never create Tk widgets); run with `python -m unittest tests.test_organizer -v` from the project root.
- **This project has no git (user's choice).** Skip all commit steps; each task ends at its verification step instead.
- Light theme (`THEMES["light"]`) must not change.
- Missing/corrupt Omarchy files must never crash the app — fall back to the built-in dark palette silently.

---

### Task 1: TOML-line parser and hex color math helpers

**Files:**
- Modify: `Organizer_Linux.py` (insert new section right after the `THEMES` dict closing brace, currently line 137)
- Test: `tests/test_organizer.py` (append new test classes at the end)

**Interfaces:**
- Produces: `_parse_simple_toml(text: str) -> dict` (str→str, only `key = "value"` lines), `_mix(hex_a: str, hex_b: str, t: float) -> str`, `_lighten(color: str, t: float) -> str`, `_darken(color: str, t: float) -> str`, module constants `_TOML_LINE_RE`, `_HEX_RE`. All hex colors are `#rrggbb` strings; `_mix` clamps `t` to [0, 1] and raises `ValueError` on invalid hex.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.ParseSimpleTomlTests tests.test_organizer.ColorMathTests -v`
Expected: ERROR on every test with `AttributeError: module 'Organizer_Linux' has no attribute '_parse_simple_toml'` (and `_mix`).

- [x] **Step 3: Write the implementation**

In `Organizer_Linux.py`, insert directly after the `THEMES = { ... }` dict (after its closing `}` on line 137), before the font comments:

```python
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
```

(`re` and `Path` are already imported at the top of the file.)

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_organizer.ParseSimpleTomlTests tests.test_organizer.ColorMathTests -v`
Expected: all 7 tests PASS (OK).

- [x] **Step 5: Run the full suite to check for regressions**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS.

---

### Task 2: `load_omarchy_palette()` and `omarchy_theme_fingerprint()`

**Files:**
- Modify: `Organizer_Linux.py` (append to the Omarchy section added in Task 1, after `_darken`)
- Test: `tests/test_organizer.py` (append)

**Interfaces:**
- Consumes: `_parse_simple_toml`, `_mix`, `_lighten`, `_darken`, `_HEX_RE`, `OMARCHY_COLORS_PATH` from Task 1.
- Produces: `load_omarchy_palette(path: Path = OMARCHY_COLORS_PATH) -> Optional[dict]` — returns a dict with exactly the same 16 keys as `THEMES["dark"]`, or `None`. `omarchy_theme_fingerprint(path: Path = OMARCHY_COLORS_PATH) -> Optional[tuple]` — `(resolved_path_str, mtime)` or `None`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_organizer.py`:

```python
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_organizer.LoadOmarchyPaletteTests tests.test_organizer.OmarchyFingerprintTests -v`
Expected: ERROR with `AttributeError: ... no attribute 'load_omarchy_palette'` / `'omarchy_theme_fingerprint'`.

- [x] **Step 3: Write the implementation**

Append to the Omarchy section in `Organizer_Linux.py` (after `_darken`):

```python
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
```

(`Optional` is already imported from `typing` at the top of the file.)

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_organizer.LoadOmarchyPaletteTests tests.test_organizer.OmarchyFingerprintTests -v`
Expected: all 7 tests PASS.

- [x] **Step 5: Run the full suite**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS.

---

### Task 3: Startup integration, WM class, and live-follow poll

**Files:**
- Modify: `Organizer_Linux.py`:
  - `main()` (currently lines 2583–2587)
  - `PhotoOrganizerApp.__init__` (poll start, after the existing `self.root.after(3000, self._poll_drives)` line, currently 1208)
  - New method `_poll_omarchy_theme` (place directly after `_toggle_theme`, currently ending line 1494)

**Interfaces:**
- Consumes: `load_omarchy_palette()`, `omarchy_theme_fingerprint()` from Task 2; existing `THEMES`, `apply_theme()`, `self._apply_theme_to_non_ttk()`, `self._configure_log_tags()`, `self.theme_name`, `self.colors`, `self.root`.
- Produces: running app whose "dark" theme is the Omarchy palette and restyles ≤2 s after `omarchy theme set`.

- [x] **Step 1: Replace `main()`**

```python
def main() -> None:
    palette = load_omarchy_palette()
    if palette is not None:
        THEMES["dark"] = palette      # "dark" *is* the Omarchy theme here
    root = tk.Tk(className="PhotoOrganizer")   # WM_CLASS for the .desktop entry
    resolve_fonts()          # swap Windows fonts for installed ones before any widgets
    PhotoOrganizerApp(root)
    root.mainloop()
```

- [x] **Step 2: Add the poll method**

Insert after `_toggle_theme` (after line 1494):

```python
    def _poll_omarchy_theme(self) -> None:
        """Re-style live when the active Omarchy theme changes on disk."""
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
            pass  # never let theme polling break the app
        self.root.after(2000, self._poll_omarchy_theme)
```

- [x] **Step 3: Start the poll in `__init__`**

After `self.root.after(3000, self._poll_drives)` (line 1208), add:

```python
        self._omarchy_fingerprint = omarchy_theme_fingerprint()
        if self._omarchy_fingerprint is not None:
            self.root.after(2000, self._poll_omarchy_theme)
```

- [x] **Step 4: Run the full test suite (regression check)**

Run: `python -m unittest tests.test_organizer -v`
Expected: all tests PASS. Also run `python -c "import Organizer_Linux"` — expected: no output, exit 0.

- [x] **Step 5: Manual verification (needs the GUI)**

1. `python3 Organizer_Linux.py` — window opens styled in Everforest colors (green-ish accent `#7fbbb3`, background `#2d353b`), not the old blue Catppuccin.
2. While it runs: `omarchy theme set "Tokyo Night"` (any installed theme) — app restyles within ~2 s. Then `omarchy theme set Everforest` to restore.
3. Toggle to Light and back to Dark via the header button — dark returns as the Omarchy palette.
4. `hyprctl clients | grep -i class` while the app runs — class is `PhotoOrganizer`.

---

### Task 4: Desktop launcher entry

**Files:**
- Create: `~/.local/share/applications/photo-organizer.desktop` (outside the project — a one-time install artifact)

**Interfaces:**
- Consumes: `StartupWMClass=PhotoOrganizer` must match the `className` set in Task 3.
- Produces: "Photo Organizer" entry in Walker / app launchers.

- [x] **Step 1: Write the desktop entry**

Create `~/.local/share/applications/photo-organizer.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Photo Organizer
Comment=Copy or move photos/videos sorted by date or type
Exec=python3 /home/nikits/Projects/Organizer/Organizer_Linux.py
Icon=folder-pictures
Terminal=false
Categories=Utility;FileTools;
StartupWMClass=PhotoOrganizer
```

- [x] **Step 2: Validate the entry**

Run: `desktop-file-validate ~/.local/share/applications/photo-organizer.desktop`
Expected: no output (valid). If `desktop-file-validate` is not installed, skip — the format above is standard.

- [x] **Step 3: Manual verification**

Run: `gio launch ~/.local/share/applications/photo-organizer.desktop`
Expected: the app window opens. Then confirm it appears in Walker (Super+Space, type "photo").
