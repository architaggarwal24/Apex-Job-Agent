# tests/test_command_palette.py
"""
tests/test_command_palette.py — the Ctrl+K command palette (the screen itself was exercised
in a real headless browser against the real API):

  * lib/palette.js under Node: ranking (every word must start a word; labels that start
    with the text first; ties keep the given order), "queue <words> [min]" parsing, and
    which Inbox jobs such a command queues (only found, fit at or above the minimum)
  * wiring: AppShell opens it with Ctrl/⌘+K, it lists every Settings section from the same
    list the Settings page uses, and its queue action goes through the normal queue route
    (owner-only, quality gate) rather than a new one

No network, no LLM. The Node part is skipped when Node.js is not installed.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class PaletteJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "palette.mjs").write_text((SRC / "lib" / "palette.js").read_text(encoding="utf-8"), encoding="utf-8")
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "palette_check.mjs").read_text(encoding="utf-8"),
                                           encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node failed: {r.stderr[-2000:]}")
        cls.out = json.loads(r.stdout)

    def test_ranking(self):
        o = self.out
        self.assertEqual(o["empty"], ["today", "apps", "inbox", "keys", "prov", "run"], "nothing typed: everything, in order")
        self.assertEqual(o["set"], ["keys", "prov"])
        self.assertEqual(o["api"], ["keys"], "every word must match")
        self.assertEqual(o["prefix_first"], ["apps", "inbox"])
        self.assertEqual(o["keyword"], ["run"], "keywords count")
        self.assertEqual(o["none"], [])
        self.assertIsNone(o["mid_word"], "a word must start with what was typed")
        self.assertEqual(o["limit"], 2)

    def test_queue_command(self):
        o = self.out
        self.assertEqual(o["pq"], {"terms": ["ai", "engineer"], "min": 70})
        self.assertEqual(o["pq_default"], {"terms": ["ai"], "min": 80})
        self.assertEqual(o["pq_plus"], {"terms": ["backend"], "min": 85})
        self.assertEqual(o["pq_fit"], {"terms": ["ai"], "min": 60})
        self.assertEqual(o["pq_clamp"], {"terms": ["ai"], "min": 100})
        self.assertEqual(o["pq_no"], [None, None, None, None])

    def test_which_jobs_it_queues(self):
        o = self.out
        self.assertEqual(o["jobs80"], ["1", "4"], "only the Inbox, fit ≥ 80, a word starting with the term (not Retail), best first")
        self.assertEqual(o["jobs75"], ["1", "4", "2"], "the company counts too (Acme AI)")
        self.assertEqual(o["jobs_old"], ["6"], "rows without fit fall back to ai_score")


class WiringTests(unittest.TestCase):
    def test_ctrl_k_opens_it_from_anywhere(self):
        shell = (SRC / "components" / "layout" / "AppShell.jsx").read_text(encoding="utf-8")
        self.assertIn("<CommandPalette", shell)
        self.assertRegex(shell, r"\(e\.ctrlKey \|\| e\.metaKey\)[^\n]*key\.toLowerCase\(\) === 'k'")

    def test_settings_sections_come_from_one_list(self):
        pal = (SRC / "components" / "layout" / "CommandPalette.jsx").read_text(encoding="utf-8")
        page = (SRC / "app" / "settings" / "page.jsx").read_text(encoding="utf-8")
        for src in (pal, page):
            self.assertIn("from '@/lib/settingsNav'", src)
        self.assertIn("SETTINGS_PLACES.map", pal)

    def test_queue_action_uses_the_normal_owner_only_route(self):
        pal = (SRC / "components" / "layout" / "CommandPalette.jsx").read_text(encoding="utf-8")
        self.assertIn("send('/api/applications/queue', 'POST'", pal)
        self.assertRegex(pal, r"id: 'a-queue-match'[^}]*ownerOnly: true")
        self.assertIn("if (item.ownerOnly && !owner)", pal)


if __name__ == "__main__":
    unittest.main()
