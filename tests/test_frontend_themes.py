"""
tests/test_frontend_themes.py — the design system, checked rather than eyeballed.

Parses the CSS that actually ships (frontend/src/app/globals.css) and checks, for
every theme: required tokens exist, text is readable (WCAG contrast — including
the translucent Glass theme over the worst-case region of its gradient),
status/chip colours are readable on their tinted backgrounds, the themes differ
in STRUCTURE (not just colour), and the gallery previews match the real CSS.
Also runs the pre-paint preferences script under Node (skipped if Node is absent).

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
CSS = (FRONT / "app" / "globals.css").read_text()
PREFS_JS = (FRONT / "lib" / "prefs.js").read_text()
THEME_JS = (FRONT / "lib" / "theme.js").read_text()


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
REQUIRED = ["--bg", "--bg2", "--panel", "--panel2", "--bdr", "--bdr-soft", "--txt", "--mut", "--acc", "--on-acc",
            "--grn", "--ylw", "--red", "--r", "--r-sm", "--bw", "--shadow", "--shadow-btn", "--blur", "--bg-image",
            "--font-body", "--font-head", "--head-weight", "--head-ls", "--label-tt", "--label-ls", "--head-shadow",
            "--nav-on-bg", "--nav-on-txt", "--nav-on-bdr", "--nav-mark"]


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


def over(fg, bg):
    """Composite fg (with alpha) over an opaque bg."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


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


def backdrops(name: str):
    """Opaque colours the theme's panel may actually sit on. For Glass that
    includes the brightest point of each gradient blob — the worst case."""
    t = THEMES[name]
    bg = over(parse_color(t["--bg"]), (255, 255, 255, 1))
    spots = [bg]
    for rgba in re.findall(r"radial-gradient\([^,]+,\s*(rgba\([^)]*\))", t["--bg-image"]):
        spots.append(over(parse_color(rgba), bg))
    return spots


class ThemeStructureTests(unittest.TestCase):
    def test_exactly_the_five_themes_exist_and_match_prefs_and_gallery(self):
        ids = re.search(r"THEME_IDS\s*=\s*\[(.*?)\]", PREFS_JS, re.S).group(1)
        ids = re.findall(r"'(\w+)'", ids)
        self.assertEqual(sorted(THEMES), sorted(ids))
        self.assertEqual(len(ids), 5)
        gallery = re.findall(r"\{\s*id:\s*'(\w+)'", THEME_JS)
        self.assertEqual(gallery, ids, "gallery order/contents must match THEME_IDS")
        self.assertEqual(ids[0], "graphite", "the first id is the fallback theme")

    def test_every_theme_defines_every_token(self):
        for name, t in THEMES.items():
            missing = [k for k in REQUIRED if k not in t]
            self.assertEqual(missing, [], f"{name} is missing {missing}")

    def test_themes_differ_in_structure_not_just_colour(self):
        structural = ["--r", "--bw", "--shadow", "--blur", "--font-head", "--font-body", "--label-tt", "--head-weight", "--nav-mark"]
        names = sorted(THEMES)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                diff = [k for k in structural if THEMES[a][k] != THEMES[b][k]]
                self.assertGreaterEqual(len(diff), 3, f"{a} vs {b} only differ in {diff} — that's a recolour")

    def test_light_and_dark_are_both_represented(self):
        bgs = {n: lum(over(parse_color(t["--bg"]), (255, 255, 255, 1))) for n, t in THEMES.items()}
        self.assertGreaterEqual(sum(v > 0.5 for v in bgs.values()), 2, bgs)
        self.assertGreaterEqual(sum(v < 0.05 for v in bgs.values()), 2, bgs)


def surfaces(name: str):
    """Every opaque colour text can end up on: the card, the page, an input/chip
    on the page, and — the stacked case that matters for translucent themes — an
    input/row INSIDE a card (panel2 composited over panel over the backdrop)."""
    t = THEMES[name]
    out = []
    for base in backdrops(name):
        panel = over(parse_color(t["--panel"]), base)
        out += [("page", base),
                ("card", panel),
                ("input on page", over(parse_color(t["--panel2"]), base)),
                ("input in card", over(parse_color(t["--panel2"]), panel))]
    return out


class ThemeContrastTests(unittest.TestCase):
    def check(self, name, label, fg, bg, minimum):
        c = contrast(fg, bg)
        self.assertGreaterEqual(c, minimum, f"{name}: {label} is {c:.2f}:1, needs {minimum}:1")

    def test_body_and_muted_text_are_readable_on_every_surface(self):
        for name, t in THEMES.items():
            txt, mut = parse_color(t["--txt"]), parse_color(t["--mut"])
            for label, surf in surfaces(name):
                self.check(name, f"text on {label}", txt, surf, 7.0)
                self.check(name, f"muted text on {label}", mut, surf, 4.5)

    def test_primary_button_text_is_readable(self):
        for name, t in THEMES.items():
            self.check(name, "on-acc on acc", parse_color(t["--on-acc"]), parse_color(t["--acc"]), 4.5)

    def test_accent_is_visible_as_a_ui_element_on_panels(self):
        # focus rings, active tab underline, switches: WCAG non-text contrast, 3:1
        for name, t in THEMES.items():
            for base in backdrops(name):
                self.check(name, "accent vs panel", parse_color(t["--acc"]), over(parse_color(t["--panel"]), base), 3.0)

    def test_status_and_chip_text_is_readable_on_its_tinted_background(self):
        # mirrors .hi/.lo/.b-wait/.mid: color-mix(<colour> 15-16%, var(--panel)) behind <colour> text
        for name, t in THEMES.items():
            if name == "brutal":
                continue                       # brutal uses explicit flat chip fills — checked below
            for base in backdrops(name):
                panel = over(parse_color(t["--panel"]), base)
                acc2 = mix(parse_color(t["--acc"]), parse_color(t["--txt"]), 0.70)
                for label, col, pct in (("grn", parse_color(t["--grn"]), .16), ("ylw", parse_color(t["--ylw"]), .16),
                                        ("red", parse_color(t["--red"]), .15), ("acc2", acc2, .16)):
                    tint = mix(parse_color(t["--acc"]) if label == "acc2" else col, panel, pct)
                    self.check(name, f"{label} chip text", col, tint, 4.5)

    def test_brutal_flat_chip_fills(self):
        t = THEMES["brutal"]
        fills = {"grn": ("#c8f5dc", t["--grn"]), "red": ("#ffd3da", t["--red"]), "ylw": ("#ffe9a8", t["--ylw"]),
                 "acc": ("#ffd1e2", "#111111")}
        for label, (fill, text) in fills.items():
            self.check("brutal", f"{label} chip", parse_color(text), parse_color(fill), 4.5)
        for sel in ("#c8f5dc", "#ffd3da", "#ffe9a8", "#ffd1e2"):
            self.assertIn(sel, CSS)

    def test_active_nav_item_is_readable(self):
        for name, t in THEMES.items():
            on_bg, on_txt = t["--nav-on-bg"], t["--nav-on-txt"]
            resolve = lambda v: parse_color({"var(--panel2)": t["--panel2"], "var(--panel)": t["--panel"],
                                              "var(--acc)": t["--acc"], "var(--txt)": t["--txt"],
                                              "var(--on-acc)": t["--on-acc"], "transparent": t["--bg"]}.get(v, v))
            for base in backdrops(name):
                bg = over(resolve(on_bg), over(parse_color(t["--bg2"]), base)) if on_bg != "transparent" \
                    else over(parse_color(t["--bg2"]), base)
                self.check(name, "active nav text", resolve(on_txt), bg, 4.5)


class AccentSwatchTests(unittest.TestCase):
    ACCENTS = re.findall(r"hex:\s*'(#[0-9a-fA-F]{6})'", PREFS_JS)

    def test_swatches_exist(self):
        self.assertEqual(len(self.ACCENTS), 8)

    def test_every_swatch_works_on_every_theme_with_the_auto_text_colour(self):
        for hex_ in self.ACCENTS:
            acc = parse_color(hex_)
            on = (255, 255, 255, 1) if contrast(acc, (255, 255, 255, 1)) >= contrast(acc, (0, 0, 0, 1)) else (0, 0, 0, 1)
            self.assertGreaterEqual(contrast(acc, on), 4.5, f"{hex_}: button text unreadable")
            for name, t in THEMES.items():
                for base in backdrops(name):
                    panel = over(parse_color(t["--panel"]), base)
                    self.assertGreaterEqual(contrast(acc, panel), 3.0, f"{hex_} invisible on {name} panel ({contrast(acc, panel):.2f})")


class GalleryPreviewTests(unittest.TestCase):
    def test_preview_colours_match_the_real_css(self):
        for m in re.finditer(r"\{\s*id:\s*'(\w+)'(.*?)\}\s*,?\s*(?=\{\s*id:|\])", THEME_JS, re.S):
            name, body = m.group(1), m.group(2)
            meta = dict(re.findall(r"(\w+):\s*'([^']*)'", body))
            css = THEMES[name]
            for js_key, css_key in (("bg", "--bg"), ("panel", "--panel"), ("bdr", "--bdr"), ("txt", "--txt"),
                                    ("mut", "--mut"), ("acc", "--acc")):
                if name == "glass" and js_key in ("panel", "bdr"):
                    continue                    # translucent: preview uses a flattened approximation
                self.assertEqual(meta[js_key].lower(), css[css_key].lower(), f"{name}.{js_key} drifted from the CSS")
            radius = int(re.search(r"radius:\s*(\d+)", body).group(1))
            css_r = float(re.match(r"([\d.]+)rem", css["--r"]).group(1)) * 16 if css["--r"] != "0" else 0
            self.assertAlmostEqual(radius, css_r, delta=2, msg=f"{name} preview radius")
            self.assertEqual(int(re.search(r"bw:\s*(\d+)", body).group(1)), int(css["--bw"].rstrip("px")))


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
            (tmp / "prefs.mjs").write_text(PREFS_JS)          # prefs.js has no imports, so it loads as-is
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "prefs_check.mjs").read_text())
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ALL PREFS", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
