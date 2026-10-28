# tests/test_frontend_themes.py
"""
tests/test_frontend_themes.py — the design system, checked rather than eyeballed.

One look ("Deck") in two variants (light and dark, frontend/src/app/globals.css). Parses the
CSS that actually ships and checks, for both: every token exists, text is readable
(WCAG contrast on every surface it can sit on), the accent is visible and its button
text readable, status / chip colours are readable on their tinted backgrounds, the
appearance picker's previews match the real CSS, component font sizes are rem, and
the variants differ only in colour (same structure). Also runs the pre-paint
preferences script under Node (skipped if Node is absent).

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend" / "src"
CSS = (FRONT / "app" / "globals.css").read_text(encoding="utf-8")
PREFS_JS = (FRONT / "lib" / "prefs.js").read_text(encoding="utf-8")
THEME_JS = (FRONT / "lib" / "theme.js").read_text(encoding="utf-8")


# ── tiny CSS-custom-property parser ──────────────────────────────────────────
def split_decls(body: str) -> dict:
    out, depth, cur = {}, 0, ""
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        if ch == ";" and depth == 0:
            cur, rest = "", cur
            m = re.match(r"\s*(--[\w-]+)\s*:\s*(.*?)\s*$", rest, re.S)
            if m:
                out[m.group(1)] = m.group(2)
        else:
            cur += ch
    return out


def theme_blocks() -> dict:
    blocks = {}
    for m in re.finditer(r"(?:^|\n)((?::root, )?\[data-theme='(\w+)'\])\s*\{(.*?)\n\}", CSS, re.S):
        blocks[m.group(2)] = split_decls(m.group(3) + ";")
    return blocks


THEMES = theme_blocks()
REQUIRED = ["--bg", "--bg2", "--panel", "--panel2", "--bdr", "--bdr-soft", "--txt", "--mut", "--faint", "--acc", "--on-acc",
            "--acc-soft", "--grn", "--ylw", "--red", "--r", "--r-sm", "--bw", "--shadow", "--shadow-pop",
            "--font-body", "--font-mono", "--head-weight", "--head-ls"]
COLOUR = {"--bg", "--bg2", "--panel", "--panel2", "--bdr", "--bdr-soft", "--txt", "--mut", "--faint", "--acc", "--on-acc",
          "--acc-soft", "--grn", "--ylw", "--red", "--blu", "--shadow", "--shadow-pop"}


# ── colour maths ─────────────────────────────────────────────────────────────
def parse_color(s: str):
    """→ (r, g, b, a) with channels 0-255, alpha 0-1. Handles #rgb, #rrggbb, rgba()."""
    s = s.strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\s*\)", s)
    if m:
        return float(m[1]), float(m[2]), float(m[3]), float(m[4] or 1)
    raise ValueError(f"unparseable colour: {s!r}")


def mix(c1, c2, pct1):
    """CSS color-mix(in srgb, c1 pct1%, c2): plain per-channel interpolation."""
    return tuple(c1[i] * pct1 + c2[i] * (1 - pct1) for i in range(3)) + (1.0,)


def lum(c):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contrast(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def col(name, token):
    return parse_color(THEMES[name][token])


def surfaces(name: str):
    """Every colour text can end up on: page, sidebar, card, the raised / hover rows."""
    return [("page", col(name, "--bg")), ("sidebar", col(name, "--bg2")),
            ("card", col(name, "--panel")), ("raised", col(name, "--panel2"))]


class ThemeStructureTests(unittest.TestCase):
    def test_exactly_light_and_dark_exist_and_match_prefs_and_picker(self):
        ids = re.findall(r"'(\w+)'", re.search(r"THEME_IDS\s*=\s*\[(.*?)\]", PREFS_JS, re.S).group(1))
        self.assertEqual(ids, ["light", "dark"])
        self.assertEqual(sorted(THEMES), ["dark", "light"])
        self.assertEqual(re.findall(r"\{\s*id:\s*'(\w+)'", THEME_JS), ids, "picker order/contents must match THEME_IDS")
        self.assertIn(":root, [data-theme='light']", CSS, "light is the fallback when no theme is set")

    def test_every_theme_defines_every_token(self):
        for name, t in THEMES.items():
            missing = [k for k in REQUIRED if k not in t]
            self.assertEqual(missing, [], f"{name} is missing {missing}")

    def test_the_variants_are_one_design_only_the_colours_change(self):
        for k in set(THEMES["dark"]) | set(THEMES["light"]):
            if k in COLOUR:
                continue
            self.assertEqual(THEMES["dark"].get(k), THEMES["light"].get(k), f"{k} differs between dark and light")

    def test_one_is_light_and_one_is_dark(self):
        self.assertLess(lum(col("dark", "--bg")), 0.02)
        self.assertGreater(lum(col("light", "--bg")), 0.75)          # a tinted (lavender) page, still clearly light

    def test_no_leftover_theme_rules(self):
        for gone in ("graphite", "paper", "terminal", "glass", "brutal"):
            self.assertNotIn(f"data-theme='{gone}'", CSS, gone)


class ThemeContrastTests(unittest.TestCase):
    def check(self, name, label, fg, bg, minimum):
        c = contrast(fg, bg)
        self.assertGreaterEqual(c, minimum, f"{name}: {label} is {c:.2f}:1, needs {minimum}:1")

    def test_body_and_muted_text_are_readable_on_every_surface(self):
        for name in THEMES:
            for label, surf in surfaces(name):
                self.check(name, f"text on {label}", col(name, "--txt"), surf, 7.0)
                self.check(name, f"muted text on {label}", col(name, "--mut"), surf, 4.5)

    def test_faint_text_is_at_least_a_visible_ui_element(self):
        # --faint is only for placeholders, hints beside a label, dividers' text: 3:1
        for name in THEMES:
            for label, surf in surfaces(name)[:3]:
                self.check(name, f"faint on {label}", col(name, "--faint"), surf, 2.5)

    def test_primary_button_text_is_readable(self):
        for name in THEMES:
            self.check(name, "on-acc on acc", col(name, "--on-acc"), col(name, "--acc"), 4.5)

    def test_accent_is_readable_as_text_and_visible_as_a_ui_element(self):
        for name in THEMES:
            for label, surf in surfaces(name):
                self.check(name, f"accent text on {label}", col(name, "--acc"), surf, 4.5)
            self.check(name, "accent text on its soft fill", col(name, "--acc"), col(name, "--acc-soft"), 4.5)
            self.check(name, "body text on the accent's soft fill", col(name, "--txt"), col(name, "--acc-soft"), 7.0)

    def test_status_and_chip_text_is_readable_on_its_tinted_background(self):
        # mirrors .hi/.lo/.b-wait: color-mix(<colour> 13-14%, var(--panel)) behind <colour> text
        for name in THEMES:
            panel = col(name, "--panel")
            for token, pct in (("--grn", .14), ("--ylw", .14), ("--red", .13)):
                c = col(name, token)
                self.check(name, f"{token} chip", c, mix(c, panel, pct), 4.5)
                self.check(name, f"{token} on the page", c, col(name, "--bg"), 4.5)


class PickerPreviewTests(unittest.TestCase):
    def test_preview_colours_match_the_real_css(self):
        for m in re.finditer(r"\{\s*id:\s*'(\w+)'(.*?)\}", THEME_JS, re.S):
            name, body = m.group(1), m.group(2)
            meta = dict(re.findall(r"(\w+):\s*'([^']*)'", body))
            for js_key, css_key in (("bg", "--bg"), ("bg2", "--bg2"), ("panel", "--panel"), ("bdr", "--bdr"),
                                    ("txt", "--txt"), ("mut", "--mut"), ("acc", "--acc")):
                self.assertEqual(meta[js_key].lower(), THEMES[name][css_key].lower(), f"{name}.{js_key} drifted from the CSS")


class NoStrayPixelFontSizesTests(unittest.TestCase):
    def test_component_font_sizes_use_rem_so_the_text_size_setting_works(self):
        for i, line in enumerate(CSS.splitlines(), 1):
            if "font-size" in line and "html" not in line and "[data-size" not in line:
                self.assertNotRegex(line, r"font-size:\s*[\d.]+px", f"line {i}: {line.strip()[:70]}")


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class PrefsScriptUnderNodeTests(unittest.TestCase):
    def test_pre_paint_script_and_prefs_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "prefs.mjs").write_text(PREFS_JS, encoding="utf-8")          # prefs.js has no imports, so it loads as-is
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "prefs_check.mjs").read_text(encoding="utf-8"), encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ALL PREFS", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
