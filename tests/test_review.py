# tests/test_review.py
"""
tests/test_review.py — structured review (applier/review.py, db/review_db.py
and their wiring through the Orchestrator):

  * packet building: what is recorded per field, where each value came from,
    the default confidences;
  * the flagging rules;
  * editing a value and what that does to the answer bank;
  * approving: the application is submitted with the REVIEWED values, honouring
    dry run; rejecting; per-user isolation and cleanup.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files; no browser, no network. The HTTP routes are in
tests/test_review_http.py.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import review
from applier import universal
from applier.review import (CONF_AI, CONF_AI_LONG, CONF_PROFILE, CONF_USER, FLAG_BELOW, NoReviewPacket,
                            apply_edit, build_fields, default_confidence, flag_reasons, merge_fields)
from core.config import cfg
from db import lifecycle
from db.answer_bank_db import AnswerBank
from db.jobs_db import JobsDB
from db.review_db import ReviewStore

LONG = "I have built and run reliable systems for many years. " * 5          # well over 200 characters


def snap(label, value="", *, type="text", key="", required=False, options=None, hit=None):
    return {"label": label, "field_key": key, "type": type, "value": value, "options": options or [],
            "required": required, "page": 1, "hit": hit}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"

    def tearDown(self):
        self.tmp.cleanup()


# ── confidence and flags ──────────────────────────────────────────

class ConfidenceTests(unittest.TestCase):
    def test_the_defaults(self):
        self.assertEqual(default_confidence("user", "text", "x"), 1.0)
        self.assertEqual(default_confidence("profile", "text", "x"), 0.95)
        self.assertEqual(default_confidence("answer_bank", "text", "x"), 1.0, "an exact bank match")
        self.assertEqual(default_confidence("answer_bank", "text", "x", matched=0.93), 0.93, "a fuzzy one keeps its ratio")
        self.assertEqual(default_confidence("ai", "text", "x"), 0.6)
        self.assertEqual((CONF_USER, CONF_PROFILE, CONF_AI, CONF_AI_LONG, FLAG_BELOW), (1.0, 0.95, 0.6, 0.4, 0.7))

    def test_ai_confidence_is_lower_for_long_free_text(self):
        self.assertEqual(default_confidence("ai", "textarea", "short"), 0.4)
        self.assertEqual(default_confidence("ai", "TEXTAREA", "short"), 0.4)
        self.assertEqual(default_confidence("ai", "text", "x" * 200), 0.4)
        self.assertEqual(default_confidence("ai", "text", "x" * 199), 0.6)

    def test_no_value_is_zero_confidence_whoever_it_is_from(self):
        for source in review.SOURCES:
            for empty in ("", "   ", None):
                self.assertEqual(default_confidence(source, "text", empty), 0.0, (source, empty))

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            default_confidence("oracle", "text", "x")


class FlagRuleTests(unittest.TestCase):
    CASES = [
        # (source, confidence, value, required, type) -> reasons
        (("profile", 0.95, "30 days", False, "text"), []),
        (("user", 1.0, "x", True, "text"), []),
        (("answer_bank", 1.0, "x", True, "text"), []),
        (("ai", 0.6, "Prefer not to say", False, "text"), ["low confidence (0.60)"]),         # < 0.7
        (("answer_bank", 0.69, "x", False, "text"), ["low confidence (0.69)"]),
        (("answer_bank", 0.70, "x", False, "text"), []),                                      # 0.7 itself is fine
        (("answer_bank", 0.88, "x", False, "text"), []),
        (("ai", 0.0, "", True, "text"), ["required but empty"]),
        (("profile", 0.0, "   ", True, "text"), ["required but empty"]),
        (("user", 0.0, "", True, "select"), ["required but empty"]),
        (("ai", 0.0, "", False, "text"), []),                                                 # optional + empty: nothing to say
        (("ai", 0.0, "", False, "textarea"), []),
        (("ai", 0.4, "short", False, "textarea"), ["AI-written free text", "low confidence (0.40)"]),
        (("ai", 0.4, LONG, False, "text"), ["AI-written free text", "low confidence (0.40)"]),
        (("ai", 0.9, LONG, False, "text"), ["AI-written free text"]),                         # flagged even if "confident"
        (("user", 1.0, LONG, False, "textarea"), []),                                         # your own essay is fine
        (("answer_bank", 1.0, LONG, False, "textarea"), []),
        (("profile", 0.95, LONG, False, "textarea"), []),
        (("ai", 0.6, "x" * 199, False, "text"), ["low confidence (0.60)"]),                   # not long enough to be prose
    ]

    def test_the_rules(self):
        for args, want in self.CASES:
            with self.subTest(args=args[:1] + args[1:4]):
                self.assertEqual(flag_reasons(*args[:4], field_type=args[4]), want)

    def test_flagged_and_reason_are_filled_in_together(self):
        f = build_fields([snap("Why us?", LONG, type="textarea")])[0]
        self.assertTrue(f["flagged"])
        self.assertEqual(f["reason"], "AI-written free text; low confidence (0.40)")
        ok = build_fields([snap("Notice period", "30 days", key="np")], {"np": "30 days"})[0]
        self.assertEqual((ok["flagged"], ok["reason"]), (False, ""))


# ── building fields ───────────────────────────────────────────────

class BuildFieldsTests(unittest.TestCase):
    def test_each_field_carries_exactly_the_documented_keys(self):
        f = build_fields([snap("Expected CTC", "20", options=["a"], required=True)])[0]
        self.assertEqual(list(f), ["id", "label", "type", "value", "source", "confidence", "flagged",
                                   "reason", "required", "options"])
        self.assertEqual((f["label"], f["type"], f["value"], f["required"], f["options"]),
                         ("Expected CTC", "text", "20", True, ["a"]))

    def test_where_a_value_came_from(self):
        answered = {"notice_period_label": "30 days", "expected_ctc_lpa": "24"}
        hit = lambda source, confidence=1.0: {"source": source, "confidence": confidence, "action": "filled",
                                              "policy": "always", "label": "x", "field_key": "k", "entry_id": 1}
        cases = [
            (snap("Notice period", "30 days", key="notice_period_label"), "profile", 0.95),
            (snap("Expected CTC", "99", key="expected_ctc_lpa"), "ai", 0.6),             # the profile says 24: this is a guess
            (snap("Valid passport", "Yes", hit=hit("user")), "answer_bank", 1.0),
            (snap("Valid passports", "Yes", hit=hit("ai", 0.93)), "answer_bank", 0.93),   # an AI-drafted saved answer, fuzzy match
            (snap("Relocate", "Yes", hit=hit("profile")), "profile", 0.95),
            (snap("Referral code", "ABC", hit=hit("application")), "user", 1.0),
            (snap("Pronouns", "Prefer not to say"), "ai", 0.6),
            (snap("Skip me", "", key="zzz"), "ai", 0.0),
        ]
        for s, source, confidence in cases:
            with self.subTest(label=s["label"]):
                f = build_fields([s], answered)[0]
                self.assertEqual((f["source"], f["confidence"]), (source, confidence))

    def test_a_seeded_profile_default_is_not_presented_as_a_profile_fact(self):
        # experience_years = "0" is in the profile as a placeholder, not in `answered`
        f = build_fields([snap("Years of experience", "0", key="experience_years")], answered={})[0]
        self.assertEqual((f["source"], f["flagged"]), ("ai", True))

    def test_without_profile_facts_nothing_is_attributed_to_the_profile(self):
        f = build_fields([snap("Notice period", "30 days", key="notice_period_label")], None)[0]
        self.assertEqual(f["source"], "ai")

    def test_ai_free_text_is_flagged_and_lower_confidence(self):
        f = build_fields([snap("Describe a project", LONG, type="textarea")])[0]
        self.assertEqual((f["source"], f["confidence"], f["flagged"]), ("ai", 0.4, True))

    def test_required_but_empty_is_flagged_and_optional_empty_is_not(self):
        req, opt = build_fields([snap("Expected CTC", "", required=True), snap("Nickname", "")])
        self.assertEqual((req["flagged"], req["reason"], req["confidence"]), (True, "required but empty", 0.0))
        self.assertEqual((opt["flagged"], opt["reason"], opt["confidence"]), (False, "", 0.0))

    def test_ids_are_readable_unique_and_stable(self):
        fields = build_fields([snap("Expected CTC (LPA) *"), snap("Name"), snap("Name"), snap("Name"),
                               snap("ट्रांसफर के लिए तैयार"), snap("x" * 200)])
        ids = [f["id"] for f in fields]
        self.assertEqual(ids[:4], ["expected-ctc-lpa", "name", "name-2", "name-3"])
        self.assertTrue(ids[4].startswith("field-"))
        self.assertEqual(len(ids[5]), 40)
        self.assertEqual(len(set(ids)), len(ids))
        again = [f["id"] for f in build_fields([snap("Expected CTC (LPA) *"), snap("Name"), snap("Name"), snap("Name"),
                                                snap("ट्रांसफर के लिए तैयार"), snap("x" * 200)])]
        self.assertEqual(ids, again)

    def test_values_are_stripped_and_options_copied(self):
        opts = ["Yes", "No"]
        f = build_fields([snap("Relocate", "  Yes  ", type="select", options=opts)])[0]
        self.assertEqual(f["value"], "Yes")
        f["options"].append("Maybe")
        self.assertEqual(opts, ["Yes", "No"])

    def test_the_packet_has_the_documented_shape(self):
        fields = build_fields([snap("Expected CTC", "20")])
        p = review.build_packet("job1", "https://example.com/apply", "resume.pdf", ["a.png"], fields)
        self.assertEqual({k: p[k] for k in ("job_id", "url", "resume", "screenshots", "fields")},
                         {"job_id": "job1", "url": "https://example.com/apply", "resume": "resume.pdf",
                          "screenshots": ["a.png"], "fields": fields})
        self.assertTrue(p["built_at"] and p["updated_at"] and p["version"] == 1)


class MergeTests(unittest.TestCase):
    def test_a_later_pass_replaces_matching_fields_keeping_ids_and_order_and_appends_new_ones(self):
        old = build_fields([snap("Expected CTC", "", required=True), snap("Notice period", "30 days"),
                            snap("Nickname", "")])
        new = build_fields([snap("What is your expected CTC?", "24 LPA", hit={"source": "application", "confidence": 1.0}),
                            snap("Brand new question", "x")])
        merged = merge_fields(old, new)
        self.assertEqual([f["label"] for f in merged],
                         ["What is your expected CTC?", "Notice period", "Nickname", "Brand new question"])
        self.assertEqual(merged[0]["id"], old[0]["id"], "the id is kept")
        self.assertEqual((merged[0]["value"], merged[0]["source"], merged[0]["flagged"]), ("24 LPA", "user", False))
        self.assertEqual(merged[1], old[1])
        self.assertEqual(len({f["id"] for f in merged}), 4)

    def test_two_fields_with_the_same_label_are_matched_one_to_one(self):
        old = build_fields([snap("Name", "A"), snap("Name", "B")])
        merged = merge_fields(old, build_fields([snap("Name", "C"), snap("Name", "D")]))
        self.assertEqual([(f["id"], f["value"]) for f in merged], [("name", "C"), ("name-2", "D")])

    def test_a_new_field_whose_id_collides_gets_a_fresh_one(self):
        old = build_fields([snap("Name", "A")])
        merged = merge_fields(old, [dict(build_fields([snap("Name!", "Z")])[0], label="Brand new")])
        self.assertEqual(len({f["id"] for f in merged}), 2)

    def test_merging_does_not_modify_its_inputs(self):
        old = build_fields([snap("Expected CTC", "")])
        before = [dict(f) for f in old]
        merge_fields(old, build_fields([snap("Expected CTC", "20")]))
        self.assertEqual(old, before)


class ReviewedAnswersTests(unittest.TestCase):
    def setUp(self):
        fields = build_fields([snap("Expected CTC", "20", key="c"), snap("Resume", "cv.pdf", type="file"),
                               snap("Notice period", "30 days"), snap("Nickname", ""), snap("?!", "x")])
        self.packet = review.build_packet("j", "u", "", [], fields)
        apply_edit(self.packet, "expected-ctc", "24 LPA")

    def test_every_non_empty_value_keyed_by_its_normalised_question(self):
        self.assertEqual(review.reviewed_answers(self.packet),
                         {"expected ctc": "24 LPA", "notice period": "30 days"})

    def test_only_user_keeps_just_what_you_set(self):
        self.assertEqual(review.reviewed_answers(self.packet, only_user=True), {"expected ctc": "24 LPA"})

    def test_no_packet_means_no_answers(self):
        self.assertEqual(review.reviewed_answers(None), {})
        self.assertEqual(review.reviewed_answers({}), {})

    def test_public_is_a_copy_with_the_flagged_count(self):
        out = review.public(self.packet)
        self.assertEqual(out["flagged"], sum(f["flagged"] for f in self.packet["fields"]))
        out["fields"][0]["value"] = "tampered"
        self.assertEqual(self.packet["fields"][0]["value"], "24 LPA")


# ── editing ───────────────────────────────────────────────────────

class ApplyEditTests(unittest.TestCase):
    def packet(self):
        return review.build_packet("j", "u", "", [], build_fields([
            snap("Expected CTC", "", required=True), snap("Why us?", LONG, type="textarea"),
            snap("Willing to relocate", "Maybe", type="select", options=["Yes", "No"]),
            snap("Notice period", "30 days", key="np")], {"np": "30 days"}))

    def test_an_edit_makes_the_value_yours_and_reworks_the_flags(self):
        p = self.packet()
        self.assertTrue(p["fields"][0]["flagged"])
        f = apply_edit(p, "expected-ctc", " 24 LPA ")
        self.assertEqual((f["value"], f["source"], f["confidence"], f["flagged"], f["reason"]),
                         ("24 LPA", "user", 1.0, False, ""), "required-but-empty is cleared by giving a value")
        f = apply_edit(p, "why", "My own words")
        self.assertEqual((f["source"], f["flagged"]), ("user", False), "AI free text you rewrote is yours")
        self.assertEqual(p["fields"][3]["source"], "profile", "other fields are untouched")

    def test_clearing_a_required_value_flags_it_again(self):
        p = self.packet()
        apply_edit(p, "expected-ctc", "24")
        f = apply_edit(p, "expected-ctc", "   ")
        self.assertEqual((f["value"], f["source"], f["confidence"], f["flagged"], f["reason"]),
                         ("", "user", 0.0, True, "required but empty"))

    def test_a_select_value_must_be_an_option_and_takes_the_options_spelling(self):
        p = self.packet()
        self.assertEqual(apply_edit(p, "willing-relocate", "yes")["value"], "Yes")
        before = dict(p["fields"][2])
        with self.assertRaises(ValueError) as cm:
            apply_edit(p, "willing-relocate", "Perhaps")
        self.assertIn("Yes, No", str(cm.exception))
        self.assertEqual(p["fields"][2], before, "a refused edit changes nothing")

    def test_too_long_and_unknown_fields_are_refused(self):
        p = self.packet()
        with self.assertRaises(ValueError):
            apply_edit(p, "expected-ctc", "x" * 5001)
        apply_edit(p, "expected-ctc", "x" * 5000)
        with self.assertRaises(KeyError):
            apply_edit(p, "nope", "x")

    def test_updated_at_moves(self):
        p = self.packet()
        before = p["updated_at"]
        apply_edit(p, "expected-ctc", "24")
        self.assertGreaterEqual(p["updated_at"], before)


class EditAndRememberTests(Base):
    def setUp(self):
        super().setUp()
        self.store = ReviewStore(1, self.path)
        self.bank = AnswerBank(1, self.path)
        self.job = {"job_id": "job1", "company": "Acme Corp"}
        self.fields = build_fields([
            snap("Expected CTC", "", required=True), snap("Why do you want to join Acme?", LONG, type="textarea"),
            snap("Describe a project", "short", type="textarea"), snap("Notice period", "30 days", key="np"),
            snap("Willing to relocate", "Maybe", type="select", options=["Yes", "No"])], {"np": "30 days"})
        self.store.save("job1", review.build_packet("job1", "https://x", "resume.pdf", ["a.png"], self.fields))

    def edit(self, field_id, value, mode="auto", store=None, bank=None, job=None):
        return review.edit_and_remember(store or self.store, bank or self.bank, job or self.job, field_id, value, mode)

    def test_the_edit_is_saved_in_the_packet_and_the_bank_per_the_default_flag(self):
        packet, field, remembered = self.edit("expected-ctc", "24 LPA")
        self.assertEqual((field["value"], field["source"]), ("24 LPA", "user"))
        self.assertEqual(self.store.get("job1")["fields"][0]["value"], "24 LPA", "persisted")
        (e,) = self.bank.entries()
        self.assertEqual((e["question_raw"], e["answer"], e["scope"], e["company"], e["source"], e["policy"]),
                         ("Expected CTC", "24 LPA", "global", "", "user", "always"))
        self.assertEqual(remembered, {"id": e["id"], "scope": "global", "company": ""})
        self.assertEqual(packet["fields"][0]["flagged"], False)

    def test_a_question_naming_the_company_or_long_free_text_is_remembered_for_the_company(self):
        _, _, r1 = self.edit("why-want-join-acme", "Because of the product")
        _, _, r2 = self.edit("describe-project", "A short essay", mode="auto")
        self.assertEqual((r1["scope"], r1["company"]), ("company", "Acme Corp"))
        self.assertEqual((r2["scope"], r2["company"]), ("company", "Acme Corp"), "a textarea is free text")

    def test_the_remember_flag_overrides(self):
        _, _, g = self.edit("why-want-join-acme", "Because", mode="global")
        _, _, c = self.edit("expected-ctc", "24 LPA", mode="company")
        self.assertEqual((g["scope"], c["scope"], c["company"]), ("global", "company", "Acme Corp"))

    def test_once_changes_the_packet_but_not_the_bank(self):
        packet, field, remembered = self.edit("expected-ctc", "24 LPA", mode="once")
        self.assertIsNone(remembered)
        self.assertEqual(self.bank.entries(), [])
        self.assertEqual(self.store.get("job1")["fields"][0]["value"], "24 LPA")

    def test_clearing_a_value_is_not_remembered(self):
        self.edit("expected-ctc", "24")
        n = len(self.bank.entries())
        _, field, remembered = self.edit("expected-ctc", "")
        self.assertEqual((field["value"], remembered, len(self.bank.entries())), ("", None, n))

    def test_editing_again_updates_the_same_entry_and_keeps_its_policy(self):
        self.bank.add("Expected CTC", "20 LPA", policy="ask")
        _, _, r = self.edit("expected-ctc", "24 LPA")
        (e,) = self.bank.entries()
        self.assertEqual((e["id"], e["answer"], e["policy"], e["source"]), (r["id"], "24 LPA", "ask", "user"))

    def test_a_refused_edit_changes_neither_the_packet_nor_the_bank(self):
        before = self.store.get("job1")
        with self.assertRaises(ValueError):
            self.edit("willing-relocate", "Perhaps")
        with self.assertRaises(KeyError):
            self.edit("nope", "x")
        self.assertEqual(self.store.get("job1"), before)
        self.assertEqual(self.bank.entries(), [])

    def test_a_label_the_bank_cannot_store_still_edits_the_packet(self):
        self.store.update("job1", lambda p: p["fields"].append(
            dict(build_fields([snap("?!", "x")])[0], id="odd")))
        _, field, remembered = self.edit("odd", "something")
        self.assertEqual((field["value"], remembered), ("something", None))

    def test_no_packet_is_its_own_error_and_not_a_missing_field(self):
        with self.assertRaises(NoReviewPacket):
            self.edit("expected-ctc", "x", job={"job_id": "other", "company": "Acme"})
        try:
            self.edit("nope", "x")
        except NoReviewPacket:
            self.fail("a missing field was reported as a missing packet")
        except KeyError:
            pass

    def test_another_user_cannot_edit_the_packet_or_touch_the_owners_bank(self):
        bob_store, bob_bank = ReviewStore(2, self.path), AnswerBank(2, self.path)
        with self.assertRaises(NoReviewPacket):
            self.edit("expected-ctc", "hacked", store=bob_store, bank=bob_bank)
        self.assertEqual(self.store.get("job1")["fields"][0]["value"], "")
        self.assertEqual((self.bank.entries(), bob_bank.entries()), ([], []))


# ── the store ─────────────────────────────────────────────────────

class StoreTests(Base):
    def test_save_get_replace_delete(self):
        s = ReviewStore(1, self.path)
        self.assertIsNone(s.get("j"))
        s.save("j", {"fields": [{"value": "héllo ✓"}], "n": 1})
        self.assertEqual(s.get("j"), {"fields": [{"value": "héllo ✓"}], "n": 1})
        s.save("j", {"n": 2})
        self.assertEqual(s.get("j"), {"n": 2})
        self.assertTrue(s.delete("j"))
        self.assertFalse(s.delete("j"))
        self.assertIsNone(s.get("j"))

    def test_update_is_atomic(self):
        s = ReviewStore(1, self.path)
        s.save("j", {"n": 1})
        packet, result = s.update("j", lambda p: (p.update(n=2), "done")[1])
        self.assertEqual((packet, result, s.get("j")), ({"n": 2}, "done", {"n": 2}))

        def boom(p):
            p["n"] = 99
            raise RuntimeError("half way")
        with self.assertRaises(RuntimeError):
            s.update("j", boom)
        self.assertEqual(s.get("j"), {"n": 2}, "a failed update saves nothing")
        self.assertIsNone(s.update("missing", lambda p: None))

    def test_one_user_cannot_see_or_change_anothers_packet(self):
        a, b = ReviewStore(1, self.path), ReviewStore(2, self.path)
        a.save("j", {"owner": "alice"})
        self.assertIsNone(b.get("j"))
        self.assertIsNone(b.update("j", lambda p: p.update(owner="bob")))
        self.assertFalse(b.delete("j"))
        b.save("j", {"owner": "bob"})                        # same job id, separate row
        self.assertEqual((a.get("j"), b.get("j")), ({"owner": "alice"}, {"owner": "bob"}))
        a.delete("j")
        self.assertEqual(b.get("j"), {"owner": "bob"})

    def test_there_is_no_unscoped_view(self):
        for bad in (None, "1", 1.5, True):
            with self.subTest(user_id=bad), self.assertRaises(ValueError):
                ReviewStore(bad, self.path)

    def test_the_table_is_created_by_the_jobs_database_too(self):
        JobsDB(path=self.path, user_id=1)
        self.assertIsNone(ReviewStore(1, self.path).get("j"))


class CleanupTests(Base):
    """A packet holds personal answers: it goes with its job."""

    def setUp(self):
        super().setUp()
        self.alice, self.bob = JobsDB(path=self.path, user_id=1), JobsDB(path=self.path, user_id=2)

    def job(self, db, n, stage_to="found"):
        jid = db.add({"url": f"https://x.com/{db.user_id}/{n}", "title": f"Role {n}", "company": f"Co {n}",
                      "source": "linkedin"})
        if stage_to == "ready_for_review":
            db.transition(jid, "preparing"); db.transition(jid, "ready_for_review")
        ReviewStore(db.user_id, self.path).save(jid, {"job": jid})
        return jid

    def test_clear_all_removes_the_packets_of_the_jobs_it_removes_and_only_those(self):
        gone = self.job(self.alice, 1)
        kept = self.job(self.alice, 2, "ready_for_review")        # in progress: clear_all keeps it
        theirs = self.job(self.bob, 3)
        self.alice.clear_all()
        a, b = ReviewStore(1, self.path), ReviewStore(2, self.path)
        self.assertEqual((a.get(gone), a.get(kept), b.get(theirs)), (None, {"job": kept}, {"job": theirs}))
        self.alice.clear_all(include_applied=True)
        self.assertIsNone(a.get(kept))
        self.assertEqual(b.get(theirs), {"job": theirs}, "Bob's packet survives Alice's clear-out")

    def test_purge_duplicates_removes_the_packets_of_purged_rows_only(self):
        with self.alice._conn() as c:
            for i in range(2):
                c.execute("""INSERT INTO jobs (job_id,url_hash,title_co_key,title,company,url,source,
                    found_at,updated_at,user_id,stage,stage_entered_at)
                    VALUES (?,?,?,?,?,?,?,?,?,1,'found','t')""",
                    (f"dup{i}", f"h{i}", "dup|co", "Dup", "Co", f"u{i}", "linkedin", "t", "t"))
        a = ReviewStore(1, self.path)
        a.save("dup0", {"n": 0}); a.save("dup1", {"n": 1})
        self.assertEqual(self.alice.purge_duplicates(), 1)
        self.assertEqual(sum(a.get(j) is not None for j in ("dup0", "dup1")), 1)


# ── through the Orchestrator ──────────────────────────────────────

class Field:
    """A FormField look-alike."""
    def __init__(self, label, key="", field_type="text", options=None, db_value="", manual=False,
                 required=False, placeholder=""):
        self.label, self.field_key, self.field_type = label, key, field_type
        self.options, self.db_value, self.needs_manual = options or [], db_value, manual
        self.is_required, self.placeholder = required, placeholder


class Analysis:
    def __init__(self, *fields):
        self.fields = list(fields)

    @property
    def missing(self):
        return [f for f in self.fields if f.needs_manual]


class FakeProfile:
    def __init__(self, answered=None):
        self.answered, self.pending = dict(answered or {}), []

    def summary(self): return {"_flat": dict(self.answered), "_answered": dict(self.answered)}
    def flag_missing(self, **kw):
        self.pending.append({"id": len(self.pending) + 1, "field_key": kw["field_key"],
                             "field_label": kw["field_label"], "field_type": kw.get("field_type", "text"),
                             "options": kw.get("options", ""), "placeholder_used": kw.get("placeholder", "")})
    def get_pending_missing(self, job_id): return list(self.pending)
    def set_many(self, *a, **k): pass
    def resolve_missing(self, *a, **k): pass


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class Playwright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class QuietSettings:
    def read(self, key, default=""): return default


class OrchestratorBase(Base):
    def setUp(self):
        super().setUp()
        self._sessions, self._resume = cfg.SESSIONS_DIR, cfg.RESUME_PATH
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.RESUME_PATH = self.dir / "resume.pdf"
        cfg.RESUME_PATH.write_bytes(b"%PDF-1.4")
        self.alice, self.bob = JobsDB(path=self.path, user_id=1), JobsDB(path=self.path, user_id=2)
        self.bank = AnswerBank(1, self.path)
        self.form = lambda: []                    # () -> [Field], the first pass
        self.refill = lambda: []                  # the second pass
        self.resubmit = lambda: []                # the final pass (LLM re-analysis at submit time)
        self.submit_seen = []                     # [(label, value)] as the submit pass filled the form
        self.submit_kwargs = []
        self.n = 0

        def fake_run(page, job, profile, emit=None, answers=None, **kw):
            a = Analysis(*self.form())
            hits = universal._apply_answers(answers, a, emit)
            return {"success": True, "screenshots": [str(self.dir / "shots" / "review_1.png")],
                    "missing_fields": a.missing, "error": "",
                    "review": {"fields": universal._snapshot(a, hits, 0), "url": "https://example.com/apply/1"}}

        def fake_iter2(page, job, profile, keys, emit=None, answers=None, **kw):
            a = Analysis(*self.refill())
            hits = universal._apply_answers(answers, a, emit)
            return {"success": True, "screenshots": [str(self.dir / "shots" / "iter2_p1.png")],
                    "missing_fields": [], "error": "",
                    "review": {"fields": universal._snapshot(a, hits, 0), "url": "https://example.com/apply/1"}}

        def fake_submit(page, job, profile, corrections=None, emit=None, dry_run=False, answers=None, **kw):
            a = Analysis(*self.resubmit())
            universal._apply_answers(answers, a, emit)
            self.submit_seen = [(f.label, f.db_value) for f in a.fields]
            self.submit_kwargs.append({"dry_run": dry_run, "corrections": corrections})
            return {"success": True, "screenshots": [], "error": ""}

        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: QuietSettings())),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                  mock.patch("applier.universal.run_application", fake_run),
                  mock.patch("applier.universal.run_iteration2", fake_iter2),
                  mock.patch("applier.universal.run_submit", fake_submit),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        cfg.SESSIONS_DIR, cfg.RESUME_PATH = self._sessions, self._resume
        super().tearDown()

    def job(self, db=None, company="Acme Corp"):
        db = db or self.alice
        self.n += 1
        jid = db.add({"url": f"https://example.com/{self.n}", "title": f"Role {self.n}",
                      "company": company, "source": "linkedin"})
        self.assertIsNotNone(jid)
        return jid

    def orchestrator(self, jid, profile=None, db=None):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id = jid
        o.job = (db or self.alice).get_by_id(jid)
        o.profile_db = profile or FakeProfile()
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(jid) or ApplicationSession(
            job_id=jid, job_url="u", job_title="t", company=o.job["company"], source="linkedin")
        o._open_browser = lambda pw: Ctx()
        return o

    def packet(self, jid, user=1):
        return ReviewStore(user, self.path).get(jid)

    def field(self, packet, field_id):
        return next(f for f in packet["fields"] if f["id"] == field_id)


class PacketBuildingTests(OrchestratorBase):
    def first_pass(self, fields, profile=None, jid=None, db=None):
        self.form = lambda: [f() for f in fields]
        jid = jid or self.job(db)
        o = self.orchestrator(jid, profile, db)
        o.run_iter1()
        return jid, o

    def test_after_the_first_pass_a_packet_is_stored_with_every_field_and_the_context(self):
        self.bank.add("Do you hold a valid passport?", "Yes")
        self.bank.add("Willing to work night shifts", "Yes")
        jid, o = self.first_pass([
            lambda: Field("Notice period", "notice_period_label", db_value="30 days"),
            lambda: Field("Do you hold a valid passport?", "passport", manual=True),
            lambda: Field("Are you willing to work night shifts?", "night", manual=True),
            lambda: Field("Preferred pronouns", "pro", placeholder="Prefer not to say"),
            lambda: Field("Why do you want this job?", "why", "textarea", db_value=LONG),
            lambda: Field("Expected CTC", "ctc", required=True),
            lambda: Field("Nickname", "nick"),
            lambda: Field("Resume", "cv", "file"),
            lambda: Field("   ", "blank", db_value="x"),
        ], FakeProfile({"notice_period_label": "30 days"}))
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")
        p = self.packet(jid)
        self.assertEqual((p["job_id"], p["url"], p["resume"], p["screenshots"]),
                         (jid, "https://example.com/apply/1", "resume.pdf", ["review_1.png"]))
        got = {f["label"]: (f["source"], f["confidence"], f["flagged"], f["reason"]) for f in p["fields"]}
        self.assertEqual(got, {
            "Notice period": ("profile", 0.95, False, ""),
            "Do you hold a valid passport?": ("answer_bank", 1.0, False, ""),
            "Are you willing to work night shifts?": ("answer_bank", got["Are you willing to work night shifts?"][1], False, ""),
            "Preferred pronouns": ("ai", 0.6, True, "low confidence (0.60)"),
            "Why do you want this job?": ("ai", 0.4, True, "AI-written free text; low confidence (0.40)"),
            "Expected CTC": ("ai", 0.0, True, "required but empty"),
            "Nickname": ("ai", 0.0, False, ""),
        })
        self.assertEqual(self.field(p, "notice-period")["value"], "30 days")
        self.assertEqual(self.field(p, "preferred-pronouns")["value"], "Prefer not to say")
        self.assertTrue(self.field(p, "expected-ctc")["required"])

    def test_a_value_you_already_gave_this_application_is_yours(self):
        from applier.session import ApplicationSession
        jid = self.job()
        s = ApplicationSession(job_id=jid, job_url="u", job_title="t", company="Acme Corp", source="linkedin")
        s.application_answers = {"referral code": "ABC123"}
        s.save()
        _, o = self.first_pass([lambda: Field("Referral code", "ref", manual=True)], jid=jid)
        f = self.packet(jid)["fields"][0]
        self.assertEqual((f["value"], f["source"], f["confidence"], f["flagged"]), ("ABC123", "user", 1.0, False))

    def test_a_job_waiting_for_your_answers_already_has_its_packet(self):
        jid, o = self.first_pass([lambda: Field("Expected CTC", "ctc", manual=True, required=True)])
        self.assertEqual(self.alice.get_stage(jid), "needs_you")
        (f,) = self.packet(jid)["fields"]
        self.assertEqual((f["value"], f["flagged"], f["reason"]), ("", True, "required but empty"))

    def test_a_review_ready_step_goes_on_the_timeline(self):
        jid, _ = self.first_pass([lambda: Field("Expected CTC", "ctc", required=True), lambda: Field("Nick", "n", db_value="x")])
        steps = [e["detail"] for e in self.alice.get_application(jid)["events"] if e["kind"] == "step"]
        step = [d for d in steps if d["step"] == "review_ready"][0]
        self.assertEqual((step["message"], step["fields"], step["flagged"]), ("Review ready: 2 fields, 2 flagged", 2, 2))

    def test_the_resume_is_blank_when_there_is_none(self):
        cfg.RESUME_PATH.unlink()
        jid, _ = self.first_pass([lambda: Field("Nick", "n", db_value="x")])
        self.assertEqual(self.packet(jid)["resume"], "")

    def test_a_second_pass_folds_your_answers_into_the_same_packet(self):
        jid, o = self.first_pass([lambda: Field("Expected CTC", "ctc", manual=True, required=True),
                                  lambda: Field("Notice period", "np", db_value="30 days")],
                                 FakeProfile({"np": "30 days"}))
        first = self.packet(jid)
        o.session.application_answers = {}
        self.refill = lambda: [Field("Expected CTC", "ctc")]
        o.run_iter2({"ctc": "24 LPA"})
        p = self.packet(jid)
        self.assertEqual([f["id"] for f in p["fields"]], [f["id"] for f in first["fields"]])
        ctc = self.field(p, "expected-ctc")
        self.assertEqual((ctc["value"], ctc["source"], ctc["flagged"]), ("24 LPA", "user", False))
        self.assertEqual(self.field(p, "notice-period"), self.field(first, "notice-period"))
        self.assertEqual((p["built_at"], p["screenshots"]), (first["built_at"], ["iter2_p1.png"]))
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")

    def test_a_failed_second_pass_does_not_reach_review_or_touch_the_packet(self):
        jid, o = self.first_pass([lambda: Field("Expected CTC", "ctc", manual=True, required=True)])
        before = self.packet(jid)
        failing = {"success": False, "screenshots": [], "missing_fields": [],
                   "error": "Validation error \u2014 the site rejected the answers: Enter a valid number"}
        with mock.patch("applier.universal.run_iteration2", lambda *a, **k: failing):
            o.run_iter2({"ctc": "twenty"})
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", "validation_error"))
        self.assertEqual(self.packet(jid), before)

    def test_a_restarted_application_gets_a_fresh_packet(self):
        jid, o = self.first_pass([lambda: Field("Old question", "q", db_value="x")])
        self.form = lambda: [Field("New question", "n", db_value="y")]
        self.orchestrator(jid).run_iter1()
        self.assertEqual([f["label"] for f in self.packet(jid)["fields"]], ["New question"])

    def test_a_problem_storing_the_packet_never_stops_the_application(self):
        self.form = lambda: [Field("Nick", "n", db_value="x")]
        jid = self.job()
        with mock.patch("db.review_db.ReviewStore.save", side_effect=RuntimeError("disk full")):
            with self.assertLogs("applier.session", "ERROR"):
                self.orchestrator(jid).run_iter1()
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")

    def test_the_packet_goes_to_the_job_owners_store(self):
        self.bank.add("Valid passport", "alice yes")
        theirs = self.job(self.bob)
        self.first_pass([lambda: Field("Valid passport", "p", manual=True)], jid=theirs, db=self.bob)
        self.assertIsNone(self.packet(theirs, user=1))
        (f,) = self.packet(theirs, user=2)["fields"]
        self.assertEqual((f["value"], f["source"]), ("", "ai"), "Bob never sees Alice's saved answer")

    def test_the_machines_profile_is_never_used_to_label_another_users_values(self):
        profile = FakeProfile({"notice_period_label": "30 days"})
        field = [lambda: Field("Notice period", "notice_period_label", db_value="30 days")]
        mine = self.first_pass(field, profile)[0]
        theirs = self.first_pass(field, profile, jid=self.job(self.bob), db=self.bob)[0]
        self.assertEqual(self.packet(mine, 1)["fields"][0]["source"], "profile")
        self.assertEqual(self.packet(theirs, 2)["fields"][0]["source"], "ai",
                         "that profile is the owner's, not Bob's")

    def test_a_legacy_job_with_no_owner_stores_under_the_first_account(self):
        legacy = JobsDB(path=self.path)
        jid = legacy.add({"url": "https://example.com/legacy", "title": "Old", "company": "Acme", "source": "x"})
        self.form = lambda: [Field("Nick", "n", db_value="x")]
        self.orchestrator(jid, db=legacy).run_iter1()
        self.assertIsNotNone(self.packet(jid, user=1))


class ApproveTests(OrchestratorBase):
    """Approving submits what you reviewed."""

    def reviewed(self, edits=None, jid=None):
        """First pass (LLM values), then review edits; returns the job id."""
        self.form = lambda: [Field("Expected CTC", "ctc", db_value="18"), Field("Notice period", "np", db_value="30 days"),
                             Field("Nickname", "nick", db_value="Sam")]
        jid = jid or self.job()
        self.orchestrator(jid, FakeProfile({"np": "30 days"})).run_iter1()
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")
        job = self.alice.get_by_id(jid)
        for fid, value in (edits or {}).items():
            review.edit_and_remember(ReviewStore(1, self.path), self.bank, job, fid, value, "once")
        # when the final pass re-reads the form the LLM now says different things
        self.resubmit = lambda: [Field("Expected CTC", "ctc", db_value="99"), Field("Notice period", "np", db_value="Immediate"),
                                 Field("Nickname", "nick", db_value="Samuel")]
        return jid

    def submit(self, jid, corrections=None, dry_run=False):
        with mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: dry_run)):
            return self.orchestrator(jid).submit(corrections)

    def test_submit_uses_the_edited_value_not_the_fresh_analysis(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        self.submit(jid)
        self.assertEqual(dict(self.submit_seen)["Expected CTC"], "24 LPA")

    def test_submit_uses_every_reviewed_value_so_what_you_approved_is_what_goes(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        self.submit(jid)
        self.assertEqual(self.submit_seen, [("Expected CTC", "24 LPA"), ("Notice period", "30 days"),
                                            ("Nickname", "Sam")],
                         "not the 'Immediate' / 'Samuel' the LLM said on the second look")

    def test_a_real_approval_marks_the_application_submitted(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        self.submit(jid)
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["status"]), ("submitted", "applied"))
        self.assertNotEqual(row["applied_at"], "")
        self.assertEqual(self.submit_kwargs, [{"dry_run": False, "corrections": None}])

    def test_a_dry_run_fills_with_the_reviewed_values_but_never_submits(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        self.submit(jid, dry_run=True)
        self.assertEqual(dict(self.submit_seen)["Expected CTC"], "24 LPA")
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["applied_at"]), ("ready_for_review", ""))
        self.assertEqual(self.submit_kwargs, [{"dry_run": True, "corrections": None}])

    def test_with_free_text_corrections_only_the_values_you_set_override_the_rest(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        with mock.patch("applier.session.Orchestrator._apply_corrections") as apply_corrections:
            self.submit(jid, corrections=["make my nickname Samuel"])     # (the LLM step that rewrites the profile)
        apply_corrections.assert_called_once()
        seen = dict(self.submit_seen)
        self.assertEqual(seen["Expected CTC"], "24 LPA", "what you edited stays")
        self.assertEqual((seen["Notice period"], seen["Nickname"]), ("Immediate", "Samuel"),
                         "everything else is re-derived, so the corrections can take effect")

    def test_an_application_reviewed_the_old_way_has_no_packet_and_submits_as_before(self):
        jid = self.job()
        self.alice.transition(jid, "preparing"); self.alice.transition(jid, "ready_for_review")
        self.resubmit = lambda: [Field("Expected CTC", "ctc", db_value="99")]
        self.submit(jid)
        self.assertEqual(self.submit_seen, [("Expected CTC", "99")])
        self.assertEqual(self.alice.get_stage(jid), "submitted")

    def test_a_problem_loading_the_packet_falls_back_to_a_fresh_analysis(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        with mock.patch("db.review_db.ReviewStore.get", side_effect=RuntimeError("locked")):
            with self.assertLogs("applier.session", "ERROR"):
                self.submit(jid)
        self.assertEqual(dict(self.submit_seen)["Expected CTC"], "99")
        self.assertEqual(self.alice.get_stage(jid), "submitted")

    def test_someone_elses_packet_for_the_same_job_id_is_never_used(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        ReviewStore(2, self.path).save(jid, review.build_packet(jid, "u", "", [], build_fields(
            [snap("Expected CTC", "EVIL")])))
        self.submit(jid)
        self.assertEqual(dict(self.submit_seen)["Expected CTC"], "24 LPA")

    def test_the_reviewed_values_are_kept_with_the_application_only(self):
        jid = self.reviewed({"expected-ctc": "24 LPA"})
        self.submit(jid)
        from applier.session import ApplicationSession
        self.assertEqual(ApplicationSession.load(jid).application_answers["expected ctc"], "24 LPA")
        self.assertEqual(self.bank.entries(), [], "approving stores nothing in the bank by itself")

    def test_an_edit_remembered_for_next_time_fills_the_next_application_too(self):
        jid = self.reviewed()
        review.edit_and_remember(ReviewStore(1, self.path), self.bank, self.alice.get_by_id(jid),
                                 "expected-ctc", "24 LPA", "auto")
        self.form = lambda: [Field("What is your expected CTC?", "c", manual=True)]
        nxt = self.job(company="Globex")
        self.orchestrator(nxt).run_iter1()
        f = self.packet(nxt)["fields"][0]
        self.assertEqual((f["value"], f["source"], f["confidence"]), ("24 LPA", "answer_bank", 1.0))


class RejectTests(OrchestratorBase):
    def in_review(self, db=None):
        self.form = lambda: [Field("Nick", "n", db_value="x")]
        jid = self.job(db)
        self.orchestrator(jid, db=db).run_iter1()
        return jid

    def test_rejecting_skips_the_application_with_the_reason_on_the_timeline(self):
        jid = self.in_review()
        review.reject_application(self.alice, jid, "Salary is too low")
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["status"], row["error_message"]), ("skipped", "skipped", "Salary is too low"))
        ev = [e for e in self.alice.get_application(jid)["events"] if e["kind"] == "transition"][-1]
        self.assertEqual((ev["from_stage"], ev["to_stage"], ev["actor"]), ("ready_for_review", "skipped", "user"))
        self.assertEqual(ev["detail"], {"review": "rejected", "note": "Salary is too low"})
        from applier.session import ApplicationSession
        self.assertEqual(ApplicationSession.load(jid).state, "skipped")

    def test_only_an_application_in_review_can_be_rejected(self):
        found = self.job()
        with self.assertRaises(lifecycle.IllegalTransition):
            review.reject_application(self.alice, found, "nope")
        self.assertEqual(self.alice.get_stage(found), "found")
        done = self.in_review()
        self.alice.transition(done, "submitted")
        with self.assertRaises(lifecycle.IllegalTransition):
            review.reject_application(self.alice, done, "too late")
        self.assertEqual(self.alice.get_stage(done), "submitted")

    def test_you_cannot_reject_someone_elses_application(self):
        jid = self.in_review(self.bob)
        with self.assertRaises(LookupError):
            review.reject_application(self.alice, jid, "mine now")
        self.assertEqual(self.bob.get_stage(jid), "ready_for_review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
