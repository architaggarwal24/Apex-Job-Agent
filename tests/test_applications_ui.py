# tests/test_applications_ui.py
"""
tests/test_applications_ui.py — the Applications workspace's plain-JS contract and wiring
(the screens themselves were exercised in a real headless browser against the real API).

  * the URL filters other pages link in with (frontend/src/lib/filters.js): parsing,
    the round trip back to a query, and what each filter keeps - run under Node;
  * the stage names it shows (frontend/src/lib/stages.js) match db/lifecycle.py;
  * /applications is one of the Next app's routes for the dev redirect;
  * the sidebar points at Applications instead of Jobs / Apply / Pipeline.

No network, no LLM. The Node part is skipped when Node.js is not installed.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import lifecycle
from server.static_shell import SHELL_ROUTES, is_public_path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "frontend" / "src" / "lib"


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class FiltersJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "stages.mjs").write_text((LIB / "stages.js").read_text(encoding="utf-8"), encoding="utf-8")
            filters = (LIB / "filters.js").read_text(encoding="utf-8").replace("'@/lib/stages'", "'./stages.mjs'")
            (tmp / "filters.mjs").write_text(filters, encoding="utf-8")
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "filters_check.mjs").read_text(encoding="utf-8"),
                                           encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node failed: {r.stderr[-2000:]}")
        cls.out = json.loads(r.stdout)

    def test_link_in_flags(self):
        o = self.out
        self.assertEqual(o["needs_you"]["stages"], ["needs_you"])
        self.assertEqual(sorted(o["both"]), ["needs_you", "ready_for_review"])
        self.assertEqual(o["stage_list"], ["found", "offer"])                 # an unknown stage is ignored
        self.assertEqual((o["search_alias"], o["search"]), ("acme", "pied"))   # search= wins over q=
        self.assertEqual((o["view_bad"], o["view_review"]), ("board", "review"))

    def test_query_round_trip(self):
        self.assertEqual(self.out["roundtrip"],
                         "?search=x+y&title=SRE&source=linkedin&stage=offer%2Cfound&min=40&max=90&view=list&job=j1")
        self.assertEqual(self.out["empty_query"], "")

    def test_what_each_filter_keeps(self):
        o = self.out
        self.assertEqual(o["f_needs_you"], "a")
        self.assertEqual(o["f_source_any_case"], "a,d")
        self.assertEqual(o["f_title_exact"], "a,c")
        self.assertEqual(o["f_search_location"], "a,d")
        self.assertEqual(o["f_score"], "a,d")
        self.assertEqual(o["f_min_only"], "a,b,d")                            # unscored is outside any range
        self.assertEqual(o["f_combined"], "a")

    def test_stage_names_match_the_lifecycle(self):
        o = self.out
        self.assertEqual(o["stages"], len(lifecycle.STAGES))
        self.assertEqual(o["post"], list(lifecycle.SUBMITTED_OR_LATER))
        self.assertEqual(o["pre"], ["found", "queued", "preparing", "needs_you", "ready_for_review"])
        self.assertEqual((o["label"], o["reason"], o["reason_unknown"]), ("Ready for review", "Login required", "some new reason"))

    def test_every_lifecycle_stage_and_reason_has_a_label(self):
        stages = (LIB / "stages.js").read_text(encoding="utf-8")
        for s in lifecycle.STAGES:
            self.assertIn(f"['{s}', ", stages, s)
        for r in lifecycle.BLOCKED_REASONS:
            self.assertRegex(stages, rf"\b{r}: '", r)


class WiringTests(unittest.TestCase):
    def test_applications_is_a_shell_route(self):
        self.assertIn("/applications", SHELL_ROUTES)
        self.assertTrue(is_public_path("/applications", None))
        self.assertTrue(is_public_path("/applications/", None))

    def test_the_page_exists(self):
        page = ROOT / "frontend" / "src" / "app" / "applications" / "page.jsx"
        self.assertTrue(page.is_file())
        text = page.read_text(encoding="utf-8")
        self.assertRegex(text, r"import \{ Suspense \} from 'react'")      # useSearchParams needs a real
        self.assertRegex(text, r"<Suspense[\s>][\s\S]*<Workspace />[\s\S]*</Suspense>")   # Suspense boundary in an export

    def test_sidebar_points_at_applications(self):
        shell = (ROOT / "frontend" / "src" / "components" / "layout" / "AppShell.jsx").read_text(encoding="utf-8")
        nav = shell[shell.index("const NAV"):shell.index("]", shell.index("const NAV"))]
        self.assertIn("'/applications'", nav)
        for gone in ("'/jobs'", "'/apply'", "'/tracking'"):
            self.assertNotIn(gone, nav)


if __name__ == "__main__":
    unittest.main()
