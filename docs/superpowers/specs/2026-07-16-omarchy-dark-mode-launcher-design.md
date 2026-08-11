# Omarchy Dark Mode + Desktop Launcher — Design

**Date:** 2026-07-16
**Status:** Approved by user

## Goal

1. The app's dark theme uses the colors of the currently active Omarchy theme
   (e.g. Everforest), and restyles live when the Omarchy theme changes while
   the app is running.
2. The app can be launched from the desktop (Walker / app launcher) via a
   `.desktop` entry instead of from a terminal.

## Constraints

- Single-file app (`Organizer_Linux.py`), Python 3.9+, standard library only.
- Must still work on machines without Omarchy: fall back to the existing
  built-in dark palette (current Catppuccin-style colors).
- Light theme is unchanged.

## Component 1: Omarchy palette loader

New module-level code in `Organizer_Linux.py`:

- `OMARCHY_COLORS_PATH = Path.home() / ".config/omarchy/current/theme/colors.toml"`
- `_parse_simple_toml(text) -> dict[str, str]` — parses `key = "#hex"` lines
  with a regex. No `tomllib` (keeps 3.9 compat). Ignores lines that don't
  match; returns `{}` on garbage.
- Color math helpers operating on `#rrggbb` strings:
  - `_mix(hex_a, hex_b, t)` — linear blend of two colors, `t` in [0, 1].
  - `_lighten(hex, t)` = mix toward white; `_darken(hex, t)` = mix toward black.
  - Invalid hex input raises `ValueError` (callers only feed validated values).
- `load_omarchy_palette() -> Optional[dict]` — reads the file, returns a full
  theme dict with the same keys as `THEMES["dark"]`, or `None` if the file is
  missing, unreadable, or lacks `background`/`foreground`.

### Palette mapping

| App key      | Source                                              |
|--------------|-----------------------------------------------------|
| `bg`         | `background`                                        |
| `bg_alt`     | darken(`bg`, 0.20)                                  |
| `surface`    | lighten(`bg`, 0.08)                                 |
| `surface_hi` | lighten(`bg`, 0.16)                                 |
| `border`     | `color0` if present, else lighten(`bg`, 0.16)       |
| `border_hi`  | lighten(`border`, 0.15)                             |
| `text`       | `foreground`                                        |
| `text_muted` | mix(`foreground`, `bg`, 0.45)                       |
| `accent`     | `accent`, else `color4`, else `foreground`          |
| `accent_hi`  | lighten(`accent`, 0.15)                             |
| `accent_lo`  | darken(`accent`, 0.15)                              |
| `accent_fg`  | `background` (dark text on the light accent)        |
| `success`    | `color2` (green), else `accent`                     |
| `warning`    | `color3` (yellow), else `accent`                    |
| `error`      | `color1` (red), else `accent`                       |
| `debug`      | mix(`foreground`, `bg`, 0.55)                       |

### Integration

At startup (in `main()` before the app builds), if `load_omarchy_palette()`
returns a palette, it replaces `THEMES["dark"]` in place. All existing code
(toggle button, profiles, `apply_theme`) keeps working untouched — "dark"
simply *is* the Omarchy palette on this machine.

## Component 2: Live-follow

- The app remembers a fingerprint of the Omarchy theme: the resolved real path
  of `~/.config/omarchy/current/theme` plus the mtime of `colors.toml`.
- A `root.after(2000, ...)` loop re-checks the fingerprint every 2 seconds.
  On change: reload the palette, update `THEMES["dark"]`, and if the current
  theme is dark, set `self.colors` and re-run `apply_theme()` +
  `_apply_theme_to_non_ttk()`.
- If Omarchy is not present at startup (`load_omarchy_palette()` returned
  `None`), the poll loop is never started.
- All filesystem access in the poll is wrapped in try/except; errors leave
  the current palette in place and the loop running.

## Component 3: Desktop launcher

File created at `~/.local/share/applications/photo-organizer.desktop`:

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

- The Tk root is created with `tk.Tk(className="PhotoOrganizer")` so the
  window's WM_CLASS matches `StartupWMClass` (proper grouping in
  Hyprland/Waybar).
- `Icon=folder-pictures` uses the stock icon theme (user-approved).
- The `.desktop` file lives outside the project; it is a one-time install
  artifact, not app code. If the project directory moves, the `Exec` path
  must be updated by hand.

## Error handling

- Missing/corrupt `colors.toml` → built-in dark palette, no crash, no dialog.
- Omarchy theme switched to a theme with a malformed `colors.toml` while the
  app runs → keep the previous palette until a valid one appears.

## Testing

- Unit tests in `tests/test_organizer.py` (headless, no Tk), matching the
  existing style:
  - `_parse_simple_toml`: valid file, empty/garbage input, quoted values.
  - `_mix` / `_lighten` / `_darken`: known blends, bounds clamping.
  - `load_omarchy_palette`: full palette from an Everforest-style fixture;
    `None` for missing file and for a file missing `background`; fallback
    chains (`accent` → `color4` → `foreground`).
- Manual verification: launch app, run `omarchy theme set <other>`, confirm
  restyle within ~2 s; launch from Walker via the new desktop entry.

## Out of scope

- Deriving a light theme from Omarchy light themes.
- Custom application icon artwork.
- Watching files with inotify (2 s polling is sufficient).
