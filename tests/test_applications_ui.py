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
class TriageJsTests(unittest.TestCase):
    """lib/triage.js: instant moves, putting back what the server refused, what Undo calls,
    the deck order and the pace estimate."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "triage.mjs").write_text((LIB / "triage.js").read_text(encoding="utf-8"), encoding="utf-8")
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "triage_check.mjs").read_text(encoding="utf-8"),
                                           encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node failed: {r.stderr[-2000:]}")
        cls.out = json.loads(r.stdout)

    def test_moves_are_instant_and_refusals_go_back(self):
        o = self.out
        self.assertEqual(o["moved"], ["skipped", "found", "skipped", "queued"])
        self.assertEqual(o["moved_at"], "2026-10-28T10:00:00.000Z")
        self.assertTrue(o["same_object"], "rows that did not move keep their identity (no needless re-render)")
        self.assertEqual(o["input_untouched"], "found")
        self.assertEqual(o["reverted"], ["skipped", "found", "needs_you", "queued"])

    def test_what_undo_calls(self):
        o = self.out
        self.assertEqual(o["undo_queue"], {"unqueue": ["a", "b"]})
        self.assertEqual(o["undo_skip"], {"restore": [{"id": "a", "to": "found"}, {"id": "d", "to": "queued"}]},
                         "a job that needed you can't be put back by a legal move, so it is not offered")
        for k in ("undo_queue_none", "undo_skip_none", "undo_other"):
            self.assertIsNone(o[k], k)

    def test_stages_match_the_lifecycle(self):
        for st in self.out["queueable"]:
            self.assertTrue(lifecycle.can_transition(st, "queued"), st)
        for st in self.out["skippable"]:
            self.assertTrue(lifecycle.can_transition(st, "skipped"), st)
        for st in ("found", "queued"):
            self.assertTrue(lifecycle.can_transition("skipped", st), f"undo of a skip needs skipped -> {st}")
        self.assertTrue(lifecycle.can_transition("queued", "found"), "undo of a queue is unqueue")

    def test_deck_order_and_pace(self):
        o = self.out
        self.assertEqual(o["deck"], ["b", "d", "c", "a"], "put-off cards go to the back, in the order put off")
        self.assertEqual(o["deck_gone"], ["b"])
        self.assertEqual(o["deck_front"], ["d", "b", "a", "c"], "undone cards go back on top, the latest first")
        self.assertEqual(o["deck_keep"], ["a", "new", "b"], "a better job streaming in does not swap the card you are reading")
        self.assertEqual(o["deck_keep_gone"], ["b"])
        self.assertEqual(o["deck_keep_undo"], ["c", "b", "a"], "an undone card still goes first")
        self.assertEqual((o["arrived"], o["arrived_first_load"]), (["n1", "n2"], []))
        self.assertEqual((o["pace_none"], o["pace_one"], o["pace"]), (None, None, 12))
        self.assertEqual((o["left"], o["left_small"], o["left_none"], o["left_done"]), (6, 1, None, None))


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class FiltersJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "stages.mjs").write_text((LIB / "stages.js").read_text(encoding="utf-8"), encoding="utf-8")
            filters = (LIB / "filters.js").read_text(encoding="utf-8").replace("'@/lib/stages'", "'./stages.mjs'")
            (tmp / "filters.mjs").write_text(filters, encoding="utf-8")
            tabs = (LIB / "tabs.js").read_text(encoding="utf-8").replace("'@/lib/stages'", "'./stages.mjs'")
            (tmp / "tabs.mjs").write_text(tabs, encoding="utf-8")
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
        self.assertEqual((o["view_bad"], o["view_review"]), ("", "review"))      # no view: the tab's own (cards for the Inbox)
        self.assertEqual(o["form_flag"], "1")

    def test_query_round_trip(self):
        self.assertEqual(self.out["roundtrip"],
                         "?search=x+y&title=SRE&source=linkedin&stage=offer%2Cfound&min=40&max=90&view=board&form=1&job=j1")
        self.assertEqual(self.out["empty_query"], "")
        self.assertEqual(self.out["roundtrip_list"], "?view=list", "an explicit list view is kept (the Inbox defaults to cards)")
        self.assertEqual(self.out["roundtrip_none"], "", "no view is not written")

    def test_what_each_filter_keeps(self):
        o = self.out
        self.assertEqual(o["f_needs_you"], "a")
        self.assertEqual(o["f_source_any_case"], "a,d")
        self.assertEqual(o["f_title_exact"], "a,c")
        self.assertEqual(o["f_search_location"], "a,d")
        self.assertEqual(o["f_score"], "a,d")
        self.assertEqual(o["f_min_only"], "a,b,d")                            # unscored is outside any range
        self.assertEqual(o["f_combined"], "a")
        self.assertEqual(o["f_form"], "a", "form=1 keeps jobs that apply on a company form")

    def test_tabs_cover_every_stage_once_and_follow_the_url(self):
        o = self.out
        self.assertEqual(sorted(o["tab_stages"]), sorted(lifecycle.STAGES), "every stage is in exactly one tab")
        self.assertEqual(len(o["tab_stages"]), len(set(o["tab_stages"])))
        self.assertEqual(o["tab_for"], {"none": "inbox", "found": "inbox", "needs": "needs", "review": "review",
                                         "interviews": "applied", "offer": "applied", "mixed": "custom", "archive": "archive"})
        self.assertEqual(o["stages_for"], {"inbox": [], "applied": list(lifecycle.SUBMITTED_OR_LATER), "nope": []})
        self.assertEqual((o["shown_empty"], o["narrowed"], o["whole"]), (["found"], True, False))

    def test_the_filter_panel_filters(self):
        p = self.out["panel"]
        self.assertEqual(p["loc"], ["p1", "p4"])
        self.assertEqual((p["work_remote"], p["work_on"]), (["p2"], ["p1", "p3", "p4"]))
        self.assertEqual(p["sal12"], ["p1", "p2", "p4"], "a job that doesn't state a salary is kept unless asked")
        self.assertEqual(p["sal12_stated"], ["p1", "p4"])
        self.assertEqual(p["nosal"], ["p1", "p3", "p4"])
        self.assertEqual((p["age3"], p["age30"]), (["p1", "p3"], ["p1", "p2", "p3"]), "posted_at, else when it was found")
        self.assertEqual((p["kind_ai"], p["kind_none_quick"]), (["p1", "p4"], ["p2", "p3"]))
        self.assertEqual((p["lvl"], p["lvl_mid"]), (["p1", "p3"], ["p2", "p4"]))
        self.assertEqual((p["co"], p["xco"], p["src"]), (["p1", "p4"], ["p1", "p2", "p4"], ["p1", "p4"]))
        self.assertEqual((p["ats_gh"], p["ats_none"], p["range"]), (["p1"], ["p2", "p4"], ["p2"]))
        self.assertEqual(p["all_none"], ["p1", "p2", "p3", "p4"])

    def test_the_filter_panel_url_and_chips(self):
        p = self.out["panel"]
        bad = p["bad_params"]
        self.assertEqual((bad["work"], bad["kind"], bad["level"], bad["age"], bad["sal"]), ("", "", "", "", ""), "junk in the URL is dropped")
        self.assertEqual(p["rt"], "?source=linkedin%2Cindeed&loc=pune&work=remote%2Chybrid&sal=12&nosal=1&age=7&kind=ai&level=senior&co=a%2Cb&xco=c&ats=lever")
        self.assertEqual(p["chips"], ["In “pune”", "Remote / Hybrid", "≥ ₹12 LPA", "Last 24 h", "Fit 60–80", "AI-scored", "Only Acme",
                                      "Not TCS", "On greenhouse / the board", "Company form"])
        self.assertEqual(p["without_range"], ["", "", "x"], "removing the fit chip clears both ends only")
        self.assertEqual(p["cleared"], ["", "", "", "", "keep", ["queued"]], "Clear all keeps the search box and the tab")

    def test_salary_seniority_and_work_mode_readers(self):
        p = self.out["panel"]
        self.assertEqual(p["sal"], [15, 18, 6, 12, None, None, 5, None])
        self.assertEqual(p["lv"], ["senior", "intern", "junior", "mid", "senior"])
        self.assertEqual(p["wk"], ["remote", "hybrid", "onsite", "remote"])
        self.assertEqual(p["ats"], ["greenhouse", "none", "none"])

    def test_stage_names_match_the_lifecycle(self):
        o = self.out
        self.assertEqual(o["stages"], len(lifecycle.STAGES))
        self.assertEqual(o["post"], list(lifecycle.SUBMITTED_OR_LATER))
        self.assertEqual(o["pre"], ["found", "queued", "preparing", "needs_you", "ready_for_review"])
        self.assertEqual((o["label"], o["reason"], o["reason_unknown"]), ("Ready for review", "Login required", "some new reason"))
        self.assertEqual(o["fit_order"], ["a", "o", "q", "n"])        # quick matches sort with AI scores; unscored last
        self.assertEqual((o["fit_min"], o["fit_of_old"]), (1, 7))

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
