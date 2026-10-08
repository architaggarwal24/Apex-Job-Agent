# tests/test_answer_bank.py
"""
tests/test_answer_bank.py — the answer bank store (db/answer_bank_db.py):
normalising questions, exact-then-fuzzy matching, scope and source
precedence, CRUD, the read-only profile entries, and per-user isolation.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db import answer_bank_db as bankdb
from db.answer_bank_db import (AnswerBank, DuplicateAnswer, compatible, names_company,
                               normalize_company, normalize_question, similarity)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.db"
        self.alice = AnswerBank(1, self.path)
        self.bob = AnswerBank(2, self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def answer(self, bank, question, company=""):
        m = bank.resolve(question, company)
        return m["answer"] if m else None


# ── normalisation ─────────────────────────────────────────────────

class NormalizationTests(unittest.TestCase):
    CASES = [
        ("What is your Notice Period?", "notice period"),
        ("Notice period (in days) *", "notice period days"),
        ("Are you willing to relocate?", "willing relocate"),
        ("  EXPECTED   CTC (LPA)  ", "expected ctc lpa"),
        ("Please enter your expected salary", "expected salary"),
        ("What's your name?", "name"),                              # apostrophe dropped, 'whats' is a stop word
        ("What’s your name?", "name"),                              # typographic apostrophe
        ("How many years of experience do you have?", "years experience"),
        ("Why do you want to work at Acme Corp?", "why want work acme corp"),   # company words are kept
        ("Current CTC (optional)", "current ctc"),
        ("Phone number *required*", "phone number"),
        ("Expected_CTC-LPA", "expected ctc lpa"),
        ("5 years of experience", "5 years experience"),            # digits survive
        ("3.5 years of experience", "3 5 years experience"),        # a decimal point is just punctuation
        ("Naïve café résumé", "naïve café résumé"),                 # accents are kept, case-folded
        ("ट्रांसफर के लिए तैयार?", "ट्रांसफर के लिए तैयार"),          # other scripts keep their vowel signs
        ("Have you never worked here before?", "never worked here before"),    # negations are never stripped
    ]
    TECH = [
        ("Years of experience in C++", "years experience cpp"),
        ("Years of experience in C#", "years experience csharp"),
        ("Years of experience in C", "years experience c"),
        ("F# developer", "fsharp developer"),
        (".NET experience", "dotnet experience"),
        ("ASP.NET experience", "aspnet experience"),
        ("Node.js experience", "nodejs experience"),
        ("React.js or Vue.js", "reactjs vuejs"),
    ]
    EMPTY = ["", "   ", None, "?!*", "the a an of", "What is your?", "(optional)"]

    def test_the_table(self):
        for raw, want in self.CASES + self.TECH:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_question(raw), want)

    def test_nothing_meaningful_normalises_to_empty(self):
        for raw in self.EMPTY:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_question(raw), "")

    def test_it_is_idempotent_and_ignores_case_spacing_and_punctuation(self):
        for raw, _ in self.CASES + self.TECH:
            once = normalize_question(raw)
            self.assertEqual(normalize_question(once), once, raw)
        a = normalize_question("What is your NOTICE   period???")
        self.assertEqual(a, normalize_question("notice period"))
        self.assertEqual(a, normalize_question("Notice Period:"))

    def test_c_plus_plus_and_c_sharp_and_c_are_three_different_questions(self):
        norms = {normalize_question(f"Years of experience in {t}") for t in ("C", "C++", "C#")}
        self.assertEqual(len(norms), 3)

    def test_company_names(self):
        for raw, want in [("Acme Corp.", "acme"), ("ACME Corporation", "acme"), ("Acme", "acme"),
                          ("Tata Consultancy Services Pvt. Ltd.", "tata consultancy services"),
                          ("AT&T Inc", "at and t"), ("Foo-Bar Labs Ltd", "foo bar labs"),
                          ("", ""), (None, ""), ("Ltd", "")]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company(raw), want)

    def test_a_question_names_a_company_by_its_words_side_by_side(self):
        yes = [("Why do you want to join Acme?", "Acme Corp"), ("Why ACME CORPORATION?", "Acme Corp."),
               ("What do you know about Tata Consultancy Services?", "Tata Consultancy Services Pvt Ltd"),
               ("Why Tata?", "Tata")]
        no = [("Why do you want to join us?", "Acme"), ("Tell us about Metals", "Meta"),
              ("What about Acmeco?", "Acme"), ("Tata Services", "Tata Consultancy Services"),
              ("anything", ""), ("", "Acme")]
        for q, c in yes:
            self.assertTrue(names_company(q, c), (q, c))
        for q, c in no:
            self.assertFalse(names_company(q, c), (q, c))


# ── fuzzy matching ────────────────────────────────────────────────

class FuzzyTests(Base):
    def setUp(self):
        super().setUp()
        self.alice.add("What is your expected salary?", "20 LPA")
        self.alice.add("Years of experience in C#", "4")
        self.alice.add("Years of experience with Python", "6")
        self.alice.add("Are you comfortable working night shifts?", "Yes")

    def test_exact_matches_are_found_through_any_phrasing_with_confidence_one(self):
        for q in ("What is your expected salary?", "expected salary", "EXPECTED SALARY *",
                  "Please enter your expected salary", "Expected salary (optional)"):
            m = self.alice.resolve(q)
            self.assertEqual((m["answer"], m["confidence"]), ("20 LPA", 1.0), q)

    def test_typos_and_small_wording_changes_match_fuzzily_with_the_ratio_as_confidence(self):
        for q in ("What is your expected salry?", "Expected salaryy", "Years of experiance in C#",
                  "Are you comfortable working night shift?"):
            m = self.alice.resolve(q)
            self.assertIsNotNone(m, q)
            self.assertLess(m["confidence"], 1.0, q)
            self.assertGreaterEqual(m["confidence"], 0.88, q)

    def test_the_confidence_is_the_difflib_ratio_rounded(self):
        q = "What is your expected salry?"
        m = self.alice.resolve(q)
        self.assertEqual(m["confidence"], round(similarity(normalize_question(q), "expected salary"), 2))

    def test_the_threshold_is_88_percent_and_inclusive(self):
        a, b = "expected salary", "expected salry"
        ratio = similarity(a, b)
        self.assertGreaterEqual(ratio, 0.88)
        self.assertIsNotNone(self.alice.resolve("Expected salry"))
        with mock.patch.object(bankdb, "FUZZY_THRESHOLD", ratio):                 # exactly the ratio: in
            self.assertIsNotNone(self.alice.resolve("Expected salry"))
        with mock.patch.object(bankdb, "FUZZY_THRESHOLD", ratio + 1e-9):          # a hair above: out
            self.assertIsNone(self.alice.resolve("Expected salry"))
        self.assertEqual(bankdb.FUZZY_THRESHOLD, 0.88)

    def test_below_the_threshold_nothing_matches(self):
        for q in ("Expected CTC", "Salary", "What salary do you want per month", "Night shift allowance",
                  "Are you comfortable"):
            with self.subTest(q=q):
                self.assertIsNone(self.alice.resolve(q))

    def test_a_different_technology_number_or_place_is_not_a_typo(self):
        """These all clear 0.88 as whole strings; the word-level guard stops them."""
        self.assertIsNone(self.alice.resolve("Years of experience in C++"))
        self.assertIsNone(self.alice.resolve("Years of experience with PyTorch"))
        self.assertTrue(similarity(normalize_question("Years of experience in C++"),
                                   normalize_question("Years of experience in C#")) >= 0.88,
                        "this pair is only safe because of the guard")
        self.alice.add("5 years of experience with Java", "Yes")
        self.assertIsNone(self.alice.resolve("6 years of experience with Java"))
        self.assertEqual(self.answer(self.alice, "5 years of experiance with Java"), "Yes")
        self.alice.add("Willing to relocate to Bangalore", "Yes")
        self.assertIsNone(self.alice.resolve("Willing to relocate to Mumbai"))

    def test_compatible_lets_typos_and_plurals_through_and_blocks_other_words(self):
        for a, b in [("notice period", "notice periods"), ("expected salary", "expected salry"),
                     ("years experience", "years experiance"), ("relocate", "relocate")]:
            self.assertTrue(compatible(a, b), (a, b))
        for a, b in [("years experience cpp", "years experience csharp"), ("5 years", "6 years"),
                     ("python experience", "pytorch experience"), ("java", "javascript")]:
            self.assertFalse(compatible(a, b), (a, b))

    def test_exact_always_beats_fuzzy_even_when_the_fuzzy_one_is_company_scoped(self):
        self.alice.add("Expected salary", "GLOBAL EXACT")
        self.alice.add("Expected salry", "company fuzzy", scope="company", company="Acme")
        self.assertEqual(self.answer(self.alice, "Expected salary", "Acme"), "GLOBAL EXACT")

    def test_among_fuzzy_candidates_the_closest_wins(self):
        bank = AnswerBank(3, self.path)
        bank.add("Notice period in days", "closer")
        bank.add("Notice periods in days", "further")
        q = "Notice period in dayz"
        near = similarity(normalize_question(q), "notice period days")
        far = similarity(normalize_question(q), "notice periods days")
        self.assertGreater(near, far)
        self.assertGreaterEqual(far, 0.88, "both must be real candidates for this test to mean anything")
        m = bank.resolve(q)
        self.assertEqual((m["answer"], m["confidence"]), ("closer", round(near, 2)))

    def test_an_empty_question_never_matches(self):
        for q in ("", "   ", "?", "the of"):
            self.assertIsNone(self.alice.resolve(q))


# ── scope and source precedence ───────────────────────────────────

class PrecedenceTests(Base):
    def test_a_company_answer_beats_the_global_one_for_that_company_only(self):
        self.alice.add("Notice period", "30 days")
        self.alice.add("Notice period", "15 days", scope="company", company="Acme Corp")
        self.assertEqual(self.answer(self.alice, "Notice period", "Acme Corp"), "15 days")
        self.assertEqual(self.answer(self.alice, "Notice period", "Globex"), "30 days")
        self.assertEqual(self.answer(self.alice, "Notice period", ""), "30 days")
        self.assertEqual(self.answer(self.alice, "Notice period"), "30 days")

    def test_the_company_is_matched_by_its_normal_form(self):
        self.alice.add("Why us?", "Because Acme", scope="company", company="Acme Corp.")
        for company in ("ACME CORPORATION", "acme", "Acme Corp"):
            self.assertEqual(self.answer(self.alice, "Why us?", company), "Because Acme", company)
        for company in ("Acme Labs", "Acmeco", "", "Globex"):
            self.assertIsNone(self.answer(self.alice, "Why us?", company), company)

    def test_a_company_answer_with_no_company_in_play_is_never_used(self):
        self.alice.add("Why us?", "Because Acme", scope="company", company="Acme")
        self.assertIsNone(self.alice.resolve("Why us?", ""))

    def test_company_beats_global_among_fuzzy_matches_even_with_a_lower_ratio(self):
        self.alice.add("Expected salary", "global, closer")
        self.alice.add("Expected salaryyyy", "company, further", scope="company", company="Acme")
        q = "Expected salaryy"
        near = similarity(normalize_question(q), "expected salary")
        far = similarity(normalize_question(q), "expected salaryyyy")
        self.assertGreater(near, far, "the global entry is the closer one")
        self.assertGreaterEqual(far, 0.88, "...and the company one is still a real fuzzy match")
        self.assertEqual(self.alice.resolve(q, "Globex")["answer"], "global, closer")     # not Acme: global only
        m = self.alice.resolve(q, "Acme")
        self.assertEqual((m["answer"], m["scope"]), ("company, further", "company"))
        self.assertEqual(self.alice.resolve("Expected salary.", "Acme")["answer"], "global, closer",
                         "an exact match still beats any fuzzy one")

    def test_your_own_answer_beats_the_ai_draft_beats_the_profile_at_the_same_scope(self):
        profile = {"notice_period_label": "Immediate"}
        self.assertEqual(self.alice.resolve("Notice period", "", profile)["source"], "profile")
        self.alice.add("Notice period", "ai guess", source="ai")
        self.assertEqual(self.alice.resolve("Notice period", "", profile)["answer"], "ai guess")
        self.alice.add("Notice period", "yours", source="user")          # same slot: updated in place
        self.assertEqual(self.alice.resolve("Notice period", "", profile)["answer"], "yours")

    def test_a_company_answer_beats_a_global_profile_fact(self):
        self.alice.add("Notice period", "15 days for Acme", scope="company", company="Acme")
        profile = {"notice_period_label": "Immediate"}
        self.assertEqual(self.alice.resolve("Notice period", "Acme", profile)["answer"], "15 days for Acme")
        self.assertEqual(self.alice.resolve("Notice period", "Globex", profile)["answer"], "Immediate")

    def test_user_beats_profile_when_one_is_global_and_the_other_is_company_scoped_ai(self):
        """scope outranks source: a company-scoped AI draft beats a global user answer."""
        self.alice.add("Notice period", "global user")
        self.alice.add("Notice period", "company ai", scope="company", company="Acme", source="ai")
        self.assertEqual(self.alice.resolve("Notice period", "Acme")["answer"], "company ai")

    def test_the_match_reports_what_it_matched(self):
        e = self.alice.add("Expected CTC", "20 LPA", scope="company", company="Acme", policy="ask")
        m = self.alice.resolve("expected ctc", "acme corp")
        self.assertEqual(m, {"answer": "20 LPA", "confidence": 1.0, "entry_id": e["id"], "source": "user",
                             "policy": "ask", "scope": "company", "question": "Expected CTC"})


# ── CRUD ──────────────────────────────────────────────────────────

class CrudTests(Base):
    def test_add_returns_the_entry_with_every_documented_field(self):
        e = self.alice.add("What is your notice period?", "30 days")
        self.assertEqual(set(e), {"id", "question_raw", "question_norm", "answer", "scope", "company",
                                  "policy", "source", "uses", "last_used_at", "created_at", "updated_at"})
        self.assertEqual((e["question_raw"], e["question_norm"], e["answer"], e["scope"], e["company"],
                          e["policy"], e["source"], e["uses"], e["last_used_at"]),
                         ("What is your notice period?", "notice period", "30 days", "global", "",
                          "always", "user", 0, None))
        self.assertNotIn("user_id", e)

    def test_saving_the_same_question_in_the_same_place_updates_it(self):
        a = self.alice.add("Notice period", "30 days")
        b = self.alice.add("what is your NOTICE period?", "45 days", policy="ask")
        self.assertEqual((b["id"], b["answer"], b["policy"], b["question_raw"]),
                         (a["id"], "45 days", "ask", "what is your NOTICE period?"))
        self.assertEqual(len(self.alice.entries()), 1)
        c1 = self.alice.add("Notice period", "x", scope="company", company="Acme")
        c2 = self.alice.add("Notice period", "y", scope="company", company="ACME Corporation")
        self.assertEqual(c1["id"], c2["id"])
        self.alice.add("Notice period", "z", scope="company", company="Globex")
        self.assertEqual(len(self.alice.entries()), 3, "global, Acme and Globex are three different places")

    def test_correcting_an_answer_never_changes_its_policy_unless_you_say_so(self):
        e = self.alice.add("Comfortable with night shifts?", "Yes", policy="ask")
        again = self.alice.add("comfortable with night shifts", "Only weekdays")          # no policy given
        self.assertEqual((again["id"], again["answer"], again["policy"]), (e["id"], "Only weekdays", "ask"))
        once = self.alice.add("Comfortable with night shifts?", "x", policy="once")        # explicit: changes it
        self.assertEqual(once["policy"], "once")
        self.assertEqual(self.alice.add("Comfortable with night shifts?", "y")["policy"], "once")
        fresh = self.alice.add("Something new", "z")
        self.assertEqual(fresh["policy"], "always", "a NEW entry defaults to always")

    def test_validation(self):
        bad = [dict(question="", answer="x"), dict(question="   ", answer="x"), dict(question="?!", answer="x"),
               dict(question="the of", answer="x"), dict(question="q" * 1001, answer="x"),
               dict(question="Notice period", answer=""), dict(question="Notice period", answer="   "),
               dict(question="Notice period", answer="a" * 5001),
               dict(question="Notice period", answer="x", scope="team"),
               dict(question="Notice period", answer="x", scope="company"),
               dict(question="Notice period", answer="x", scope="company", company="Ltd"),
               dict(question="Notice period", answer="x", policy="sometimes"),
               dict(question="Notice period", answer="x", source="profile"),
               dict(question="Notice period", answer="x", source="robot")]
        for kw in bad:
            with self.subTest(kw=str(kw)[:60]), self.assertRaises(ValueError):
                self.alice.add(**kw)
        self.assertEqual(self.alice.entries(), [])
        self.alice.add("Notice period", "x" * 5000)                     # the limits are inclusive
        self.alice.add("q" * 1000, "ok")
        self.assertEqual(len(self.alice.entries()), 2)

    def test_a_global_answer_forgets_any_company_it_was_given(self):
        e = self.alice.add("Notice period", "x", scope="global", company="Acme")
        self.assertEqual(e["company"], "")

    def test_source_profile_cannot_be_written_but_ai_can(self):
        with self.assertRaises(ValueError) as cm:
            self.alice.add("Notice period", "x", source="profile")
        self.assertIn("read-only", str(cm.exception))
        self.assertEqual(self.alice.add("Notice period", "x", source="ai")["source"], "ai")

    def test_get_update_delete(self):
        e = self.alice.add("Notice period", "30 days", source="ai")
        self.assertEqual(self.alice.get(e["id"])["answer"], "30 days")
        self.assertIsNone(self.alice.get(9999))
        u = self.alice.update(e["id"], answer="45 days")
        self.assertEqual((u["answer"], u["source"]), ("45 days", "user"),
                         "editing the answer makes it yours: you vouch for it")
        u = self.alice.update(e["id"], policy="ask")
        self.assertEqual((u["policy"], u["source"]), ("ask", "user"))
        u = self.alice.update(e["id"], scope="company", company="Acme")
        self.assertEqual((u["scope"], u["company"]), ("company", "Acme"))
        u = self.alice.update(e["id"], scope="global")
        self.assertEqual((u["scope"], u["company"]), ("global", ""))
        u = self.alice.update(e["id"], question="Joining time")
        self.assertEqual((u["question_raw"], u["question_norm"]), ("Joining time", "joining time"))
        self.assertIsNone(self.alice.update(9999, answer="x"))
        self.assertTrue(self.alice.delete(e["id"]))
        self.assertFalse(self.alice.delete(e["id"]))
        self.assertIsNone(self.alice.get(e["id"]))

    def test_an_edit_that_changes_only_the_policy_keeps_the_source(self):
        e = self.alice.add("Notice period", "x", source="ai")
        self.assertEqual(self.alice.update(e["id"], policy="once")["source"], "ai")

    def test_update_refuses_bad_values_and_collisions_and_unknown_fields(self):
        a = self.alice.add("Notice period", "30 days")
        b = self.alice.add("Expected CTC", "20 LPA")
        with self.assertRaises(DuplicateAnswer):
            self.alice.update(b["id"], question="Notice period")
        for kw in (dict(answer=""), dict(scope="company"), dict(policy="x"), dict(question=""),
                   dict(scope="nope")):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.alice.update(a["id"], **kw)
        with self.assertRaises(ValueError):
            self.alice.update(a["id"], source="profile")
        with self.assertRaises(ValueError):
            self.alice.update(a["id"], uses=99)
        self.assertEqual(self.alice.get(a["id"])["answer"], "30 days")
        # the same question may exist in another scope
        self.alice.update(a["id"], scope="company", company="Acme")
        self.assertEqual(len(self.alice.entries()), 2)

    def test_listing_and_filters(self):
        self.alice.add("Notice period", "30 days")
        self.alice.add("Why Acme?", "Because", scope="company", company="Acme Corp")
        self.alice.add("Expected CTC", "20 LPA", source="ai")
        names = lambda **kw: sorted(e["question_raw"] for e in self.alice.entries(**kw))
        self.assertEqual(names(), ["Expected CTC", "Notice period", "Why Acme?"])
        self.assertEqual(names(scope="company"), ["Why Acme?"])
        self.assertEqual(names(source="ai"), ["Expected CTC"])
        self.assertEqual(names(q="notice"), ["Notice period"])
        self.assertEqual(names(q="20 lpa"), ["Expected CTC"], "q searches answers too")
        self.assertEqual(names(company="ACME CORPORATION"), ["Why Acme?"])
        self.assertEqual(names(company="Globex"), [])
        self.assertEqual(len(self.alice.entries(limit=2)), 2)
        self.assertEqual(len(self.alice.entries(limit=0)), 1, "limit is clamped to at least 1")
        for bad in (dict(scope="x"), dict(source="x")):
            with self.assertRaises(ValueError):
                self.alice.entries(**bad)

    def test_like_wildcards_in_the_search_are_literal(self):
        self.alice.add("Salary 100% fixed", "yes")
        self.alice.add("Salary other", "no")
        self.assertEqual([e["question_raw"] for e in self.alice.entries(q="100%")], ["Salary 100% fixed"])
        self.assertEqual(self.alice.entries(q="%"), self.alice.entries(q="100%"))
        self.assertEqual(self.alice.entries(q="_"), [])

    def test_the_most_recently_used_comes_first(self):
        a = self.alice.add("Notice period", "x"); b = self.alice.add("Expected CTC", "y")
        self.assertEqual([e["id"] for e in self.alice.entries()], [b["id"], a["id"]])
        self.alice.touch(a["id"])
        self.assertEqual([e["id"] for e in self.alice.entries()], [a["id"], b["id"]])

    def test_uses_and_last_used_at_count_only_real_uses(self):
        e = self.alice.add("Notice period", "30 days")
        self.alice.resolve("Notice period"); self.alice.resolve("Notice period")
        fuzzy = self.alice.resolve("Notice periods")
        self.assertEqual(fuzzy["entry_id"], e["id"], "this one is a fuzzy match, not an exact one")
        self.assertLess(fuzzy["confidence"], 1.0)
        self.assertEqual(self.alice.get(e["id"])["uses"], 0, "looking is not using")
        self.alice.touch(e["id"]); self.alice.touch(e["id"])
        got = self.alice.get(e["id"])
        self.assertEqual(got["uses"], 2)
        self.assertTrue(got["last_used_at"])

    def test_the_database_itself_refuses_invalid_enum_values(self):
        with closing(sqlite3.connect(str(self.path))) as c:        # closing(): `with conn` alone never closes it
            for col, bad in (("scope", "team"), ("policy", "sometimes"), ("source", "robot")):
                vals = dict(scope="global", policy="always", source="user")
                vals[col] = bad
                with self.assertRaises(sqlite3.IntegrityError, msg=col):
                    c.execute("INSERT INTO answer_bank (user_id, question_raw, question_norm, answer, scope,"
                              " policy, source, created_at, updated_at) VALUES (1,'q','q','a',?,?,?,'t','t')",
                              (vals["scope"], vals["policy"], vals["source"]))

    def test_the_table_survives_being_opened_again(self):
        self.alice.add("Notice period", "30 days")
        self.assertEqual(len(AnswerBank(1, self.path).entries()), 1)


# ── profile-derived entries ───────────────────────────────────────

class ProfileTests(Base):
    PROFILE = {"notice_period_label": "30 days", "current_ctc_lpa": "18", "expected_ctc_lpa": "24",
               "willing_to_relocate": "Yes", "experience_years": "6", "work_mode_preference": "Hybrid",
               "linkedin_url": "https://linkedin.com/in/x", "github_url": "", "full_name": "Not a question"}

    def test_the_profile_facts_with_values_answer_their_usual_phrasings(self):
        cases = [("What is your notice period?", "30 days"), ("How soon can you join?", "30 days"),
                 ("Current CTC (LPA)", "18"), ("Current salary", "18"), ("Expected CTC", "24"),
                 ("Salary expectation", "24"), ("Are you willing to relocate?", "Yes"),
                 ("Total years of experience", "6"), ("How many years of experience do you have?", "6"),
                 ("Preferred work mode", "Hybrid"), ("LinkedIn profile", "https://linkedin.com/in/x")]
        for q, want in cases:
            with self.subTest(q=q):
                m = self.alice.resolve(q, "", self.PROFILE)
                self.assertIsNotNone(m, q)
                self.assertEqual((m["answer"], m["source"], m["policy"], m["scope"], m["confidence"]),
                                 (want, "profile", "always", "global", 1.0))
                self.assertTrue(m["entry_id"].startswith("profile:"))

    def test_empty_or_unknown_profile_keys_give_nothing(self):
        self.assertIsNone(self.alice.resolve("GitHub profile", "", self.PROFILE))     # empty value
        self.assertIsNone(self.alice.resolve("What is your full name?", "", self.PROFILE))
        self.assertIsNone(self.alice.resolve("Notice period", "", {}))
        self.assertIsNone(self.alice.resolve("Notice period", "", None))
        self.assertIsNone(self.alice.resolve("Notice period"))

    def test_notice_period_and_notice_period_in_days_are_different_questions(self):
        p = {"notice_period_label": "Immediate", "notice_period_days": "0"}
        self.assertEqual(self.alice.resolve("Notice period", "", p)["answer"], "Immediate")
        self.assertEqual(self.alice.resolve("Notice period in days", "", p)["answer"], "0")
        p2 = {"current_ctc_lpa": "18", "current_ctc_monthly": "150000"}
        self.assertEqual(self.alice.resolve("Current salary", "", p2)["answer"], "18")
        self.assertEqual(self.alice.resolve("Current monthly salary", "", p2)["answer"], "150000")

    def test_profile_entries_are_listed_read_only_with_ids_you_cannot_edit(self):
        self.alice.add("Why Acme?", "Because", scope="company", company="Acme")
        rows = self.alice.entries_with_profile(self.PROFILE)
        profile_rows = [r for r in rows if r["source"] == "profile"]
        self.assertTrue(profile_rows)
        for r in profile_rows:
            self.assertTrue(r["read_only"] and r["id"].startswith("profile:"), r)
            self.assertEqual((r["scope"], r["policy"], r["uses"]), ("global", "always", 0))
            self.assertNotIn("phrasings", r)
        self.assertEqual({r["id"] for r in profile_rows},
                         {"profile:notice_period_label", "profile:current_ctc_lpa", "profile:expected_ctc_lpa",
                          "profile:willing_to_relocate", "profile:experience_years",
                          "profile:work_mode_preference", "profile:linkedin_url"})
        self.assertEqual(len(rows), len(profile_rows) + 1)

    def test_profile_entries_respect_the_listing_filters(self):
        self.alice.add("Why Acme?", "Because", scope="company", company="Acme")
        ids = lambda **kw: {r["id"] for r in self.alice.entries_with_profile(self.PROFILE, **kw)}
        self.assertEqual(ids(source="profile"), ids() - {r["id"] for r in self.alice.entries()})
        self.assertEqual(ids(scope="company"), {r["id"] for r in self.alice.entries(scope="company")})
        self.assertEqual(ids(company="Acme"), {r["id"] for r in self.alice.entries(company="Acme")})
        self.assertEqual(ids(q="notice"), {"profile:notice_period_label"})
        self.assertEqual(ids(source="user"), {r["id"] for r in self.alice.entries()})
        self.assertEqual({r["id"] for r in self.alice.entries_with_profile(None)},
                         {r["id"] for r in self.alice.entries()})

    def test_the_profile_owner_is_the_first_account(self):
        self.assertEqual(bankdb.PROFILE_OWNER_ID, 1)


class ProfileAnsweredTests(Base):
    """Only facts a person actually provided count as profile answers: the
    profile is seeded with placeholders (experience_years = 0 ...)."""

    def setUp(self):
        super().setUp()
        from db.profile_db import ProfileDB
        self.profile = ProfileDB(Path(self.tmp.name) / "profile.db")

    def test_a_fresh_profile_has_no_answers_even_though_it_has_seeded_values(self):
        self.assertTrue(any(v for v in self.profile.get_all().values()), "the seeds exist")
        self.assertEqual(self.profile.answered_values(), {})
        self.assertIsNone(self.alice.resolve("Total years of experience", "", self.profile.answered_values()),
                          "a placeholder 0 must never be offered as an answer")
        self.assertEqual(bankdb.profile_entries(self.profile.answered_values()), [])

    def test_what_you_set_counts_and_empty_values_do_not(self):
        self.profile.set("experience_years", "6")                  # a seeded field you changed
        self.profile.set("expected_ctc_lpa", "24")
        self.profile.set("my_custom_fact", "yes")                  # a field the seeds don't know
        self.profile.set("current_ctc_lpa", "")
        self.assertEqual(self.profile.answered_values(),
                         {"experience_years": "6", "expected_ctc_lpa": "24", "my_custom_fact": "yes"})

    def test_a_seeded_field_you_changed_and_changed_back_still_counts(self):
        self.profile.set("notice_period_label", "30 days")
        self.profile.set("notice_period_label", "Immediate")      # back to the default, but you have touched it
        self.assertEqual(self.profile.answered_values().get("notice_period_label"), "Immediate")

    def test_the_bank_answers_from_them_and_the_summary_carries_them(self):
        self.profile.set("notice_period_label", "45 days")
        answered = self.profile.answered_values()
        self.assertEqual(self.profile.summary()["_answered"], answered)
        m = self.alice.resolve("What is your notice period?", "", answered)
        self.assertEqual((m["answer"], m["source"]), ("45 days", "profile"))


# ── isolation ─────────────────────────────────────────────────────

class IsolationTests(Base):
    def test_one_users_answers_are_invisible_to_another(self):
        mine = self.alice.add("Expected CTC", "20 LPA")
        self.assertEqual(self.bob.entries(), [])
        self.assertIsNone(self.bob.get(mine["id"]))
        self.assertIsNone(self.bob.resolve("Expected CTC"))
        self.assertIsNone(self.bob.resolve("Expected CTC", "Acme"))
        self.assertEqual(self.bob.entries(q="CTC"), [])

    def test_one_user_cannot_change_delete_or_count_anothers_entry(self):
        mine = self.alice.add("Expected CTC", "20 LPA")
        self.assertIsNone(self.bob.update(mine["id"], answer="hacked"))
        self.assertFalse(self.bob.delete(mine["id"]))
        self.bob.touch(mine["id"])
        got = self.alice.get(mine["id"])
        self.assertEqual((got["answer"], got["uses"]), ("20 LPA", 0))

    def test_the_same_question_is_a_separate_entry_per_user(self):
        a = self.alice.add("Expected CTC", "20 LPA")
        b = self.bob.add("Expected CTC", "35 LPA")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.answer(self.alice, "Expected CTC"), "20 LPA")
        self.assertEqual(self.answer(self.bob, "Expected CTC"), "35 LPA")
        self.bob.delete(b["id"])
        self.assertEqual(self.answer(self.alice, "Expected CTC"), "20 LPA")

    def test_company_scoped_answers_are_per_user_too(self):
        self.alice.add("Why us?", "alice's essay", scope="company", company="Acme")
        self.assertIsNone(self.bob.resolve("Why us?", "Acme"))

    def test_there_is_no_unscoped_view(self):
        for bad in (None, "1", 1.0, True, "", 0.5):
            with self.subTest(user_id=bad), self.assertRaises(ValueError):
                AnswerBank(bad, self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
