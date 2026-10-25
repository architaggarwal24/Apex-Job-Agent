# tests/test_kits.py
"""
tests/test_kits.py — resume variants and the per-application kit.

  * upload validation: size, the PDF signature, label, tags, the limits, the
    default; a rejected upload leaves nothing behind;
  * encryption at rest: the file on disk is not the PDF, it round-trips, the
    extracted text is encrypted too, a wrong key or a plaintext file is refused,
    plaintext exists only in a temp file while a form is being filled;
  * choosing the resume: none / one / no description / the LLM / fallback to the
    default whenever the LLM is down or answers nonsense;
  * the kit: a 3-sentence summary and a cover letter (the ghostwriter), versions,
    the person's override (kept by regenerations), and a kit failure never raising;
  * isolation between users, for resumes, kits and what is sent to the LLM;
  * the form fillers upload the CHOSEN resume (universal filler and adapters),
    and the Orchestrator prepares the kit during `preparing` without ever being
    blocked by it.

The LLM is a fake (core.llm.chat / chat_json are patched); no network.
The HTTP routes' tests are in the last class and skip without FastAPI.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import contextlib
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import kit as kits
from core import crypto
from core.config import cfg
from db import resumes_db
from db.jobs_db import JobsDB
from db.kits_db import KitStore
from db.resumes_db import (MAX_BYTES, MAX_VARIANTS, ResumeError, ResumeSource, ResumeStore, ResumeTooLarge,
                           safe_filename, sweep_stale_temp)

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


def make_pdf(text: str = "Resume") -> bytes:
    """A small valid PDF with `text` on its page (pdfplumber reads it back)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)


BACKEND = make_pdf("Backend engineer Python Postgres Kafka")
DATA    = make_pdf("Data scientist PyTorch Spark")
MANAGER = make_pdf("Engineering manager hiring roadmaps")


class FakeLLM:
    """core.llm.chat / chat_json. `pick` is what the selection call returns: a
    dict, a callable(prompt) -> dict, or an Exception to raise."""
    def __init__(self):
        self.calls = []
        self.pick = None
        self.summary = "I build backend systems. I work mostly in Python and Postgres. I ship carefully."
        self.letter = "Dear Acme team, I would love to join you."
        self.text_fails = False

    def chat_json(self, messages, system="", max_tokens=4096):
        prompt = messages[0]["content"]
        self.calls.append(("select", system, prompt))
        pick = self.pick(prompt) if callable(self.pick) else self.pick
        if isinstance(pick, Exception):
            raise pick
        return pick

    def chat(self, messages, system="", max_tokens=4096, temperature=None, expect_json=False):
        prompt = messages[0]["content"]
        kind = "summary" if "three sentences" in prompt else "letter"
        self.calls.append((kind, system, prompt))
        if self.text_fails:
            raise RuntimeError("LLM call failed (fake): key sk-SECRET-123 rejected")
        return self.summary if kind == "summary" else self.letter

    def kinds(self):
        return [c[0] for c in self.calls]

    def prompts(self, kind):
        return [c[2] if kind == "select" else c[1] for c in self.calls if c[0] == kind]


PROFILE = {"_flat": {"full_name": "Priya Sharma", "current_role": "Backend engineer", "experience_years": "6"},
           "skills": ["python", "postgres"]}
DESCRIPTION = "We need a backend engineer to build Python services on Postgres and Kafka at scale."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self.root = self.dir / "resumes"
        self.llm = FakeLLM()
        (self.dir / "tmp").mkdir()                 # decrypted temp resumes go here, never into the real /tmp
        for p in (mock.patch("core.llm.chat", self.llm.chat), mock.patch("core.llm.chat_json", self.llm.chat_json),
                  mock.patch.object(tempfile, "tempdir", str(self.dir / "tmp")),
                  mock.patch.object(cfg, "DATA_DIR", self.dir),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)
        self.n = 0

    def resumes(self, uid=1):
        return ResumeStore(uid, self.path, self.root)

    def upload(self, label="Backend", pdf=BACKEND, uid=1, tags=None, **kw):
        return self.resumes(uid).add(f"{label}.pdf", pdf, label, tags, **kw)

    def job(self, uid=1, description=DESCRIPTION, title="Backend Engineer"):
        self.n += 1
        db = JobsDB(path=self.path, user_id=uid)
        jid = db.add({"url": f"https://example.com/{uid}/{self.n}", "title": f"{title} {self.n}", "company": "Acme",
                      "source": "linkedin", "description": description})
        self.assertIsNotNone(jid)
        db.transition(jid, "preparing")
        return db.get_by_id(jid)

    def kit(self, job, **kw):
        kw.setdefault("profile", PROFILE)
        return kits.prepare_kit(job, path=self.path, root=self.root, **kw)

    def kit_store(self, uid=1):
        return KitStore(uid, self.path)


# ── upload validation ─────────────────────────────────────────────

class UploadValidationTests(Base):
    def rejected(self, data=BACKEND, label="Backend", tags=None, exc=ResumeError, filename="r.pdf"):
        with self.assertRaises(exc) as cm:
            self.resumes().add(filename, data, label, tags)
        return cm.exception

    def test_a_pdf_is_accepted_and_described(self):
        v = self.upload("Backend", tags="Python, Postgres")
        self.assertEqual((v["label"], v["tags"], v["filename"], v["size"], v["is_default"]),
                         ("Backend", ["python", "postgres"], "Backend.pdf", len(BACKEND), True))
        self.assertNotIn("stored_name", v)
        self.assertNotIn("text", v)

    def test_the_size_limit_is_5_mb_inclusive(self):
        ok = b"%PDF-1.4\n" + b"0" * (MAX_BYTES - 9)
        self.assertEqual(len(ok), MAX_BYTES)
        self.assertEqual(self.upload("Exactly", ok)["size"], MAX_BYTES)
        e = self.rejected(ok + b"0", label="Over", exc=ResumeTooLarge)
        self.assertEqual(e.status, 413)

    def test_empty_files_are_refused_with_a_clear_message(self):
        self.assertIn("empty", str(self.rejected(b"")))
        self.assertIn("not a PDF", str(self.rejected(b"x")))

    def test_it_must_start_with_the_pdf_signature_whatever_it_is_called(self):
        self.rejected(b"\x89PNG\r\n\x1a\n" + b"0" * 100, filename="resume.pdf")
        self.rejected(b"just some text", filename="resume.pdf")
        self.rejected(b" %PDF-1.4 leading space", filename="resume.pdf")
        self.rejected(b"\xef\xbb\xbf%PDF-1.4 with a BOM", filename="resume.pdf")
        self.rejected(b"PK\x03\x04 a zip with a pdf name", filename="resume.pdf")
        self.assertEqual(self.upload("Odd name", BACKEND)["filename"], "Odd name.pdf")     # the bytes decide, not the name
        self.assertEqual(self.resumes().add("cv.txt", BACKEND, "Named txt")["filename"], "cv.txt.pdf")

    def test_a_label_is_required_and_short(self):
        for bad in ("", "   ", None, 5, "x" * 61):
            self.rejected(label=bad)
        self.assertEqual(self.upload("  Back   end  ")["label"], "Back end")

    def test_a_label_is_unique_per_user_ignoring_case(self):
        self.upload("Backend")
        e = self.rejected(label="BACKEND")
        self.assertIn("already", str(e))

    def test_tags_are_tidied_and_limited(self):
        self.assertEqual(self.upload("A", tags=[" Python ", "python", "", "Go "])["tags"], ["python", "go"])
        self.assertEqual(self.upload("B", tags="a, b ,, a")["tags"], ["a", "b"])
        self.assertEqual(self.upload("C", tags=None)["tags"], [])
        self.rejected(label="D", tags=["x" * 31])
        self.rejected(label="E", tags=[str(i) for i in range(11)])
        self.rejected(label="F", tags=[1, 2])
        self.rejected(label="G", tags={"a": 1})

    def test_at_most_ten_resumes_and_a_deleted_one_frees_a_place(self):
        for i in range(MAX_VARIANTS):
            self.upload(f"R{i}")
        e = self.rejected(label="One too many")
        self.assertIn(str(MAX_VARIANTS), str(e))
        self.resumes().delete(self.resumes().list()[-1]["id"])
        self.upload("Fits now")

    def test_a_rejected_upload_leaves_nothing_behind(self):
        self.upload("Keep")
        before = sorted(p.name for p in (self.root / "1").iterdir())
        for data, label in ((b"nope", "X"), (b"", "X"), (BACKEND, ""), (BACKEND, "Keep")):
            with self.assertRaises(ResumeError):
                self.resumes().add("r.pdf", data, label)
        self.assertEqual(sorted(p.name for p in (self.root / "1").iterdir()), before)
        self.assertEqual(len(self.resumes().list()), 1)

    def test_the_uploaded_name_is_never_used_on_disk(self):
        v = self.resumes().add("../../etc/passwd.pdf", BACKEND, "Sneaky")
        self.assertEqual(v["filename"], "passwd.pdf")
        files = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, self.root / "1")
        self.assertRegex(files[0].name, r"^[0-9a-f]{32}\.pdf\.enc$")
        self.assertEqual(safe_filename("C:\\Users\\me\\My Résumé (final).pdf"), "My R_sum_ _final_.pdf")
        self.assertEqual(safe_filename(""), "resume.pdf")
        self.assertEqual(safe_filename("..."), "resume.pdf")

    def test_a_store_needs_a_user_id(self):
        for bad in (None, "1", True):
            with self.assertRaises(ValueError):
                ResumeStore(bad, self.path, self.root)


class DefaultTests(Base):
    def test_the_first_resume_is_the_default(self):
        a, b = self.upload("A"), self.upload("B", DATA)
        self.assertEqual((a["is_default"], b["is_default"]), (True, False))
        self.assertEqual(self.resumes().default()["label"], "A")

    def test_make_default_on_upload_moves_the_default(self):
        self.upload("A")
        b = self.upload("B", DATA, make_default=True)
        self.assertEqual([v["is_default"] for v in self.resumes().list()], [False, True])
        self.assertEqual(self.resumes().default()["id"], b["id"])

    def test_set_default_leaves_exactly_one(self):
        a, b, c = self.upload("A"), self.upload("B", DATA), self.upload("C", MANAGER)
        self.assertEqual(self.resumes().set_default(c["id"])["is_default"], True)
        self.assertEqual([v["is_default"] for v in self.resumes().list()], [False, False, True])
        self.assertIsNone(self.resumes().set_default(999))

    def test_deleting_the_default_promotes_the_oldest_remaining(self):
        a, b, c = self.upload("A"), self.upload("B", DATA), self.upload("C", MANAGER)
        self.resumes().set_default(c["id"])
        self.assertTrue(self.resumes().delete(c["id"]))
        self.assertEqual(self.resumes().default()["id"], a["id"])
        self.resumes().delete(a["id"])
        self.assertEqual(self.resumes().default()["id"], b["id"])
        self.resumes().delete(b["id"])
        self.assertIsNone(self.resumes().default())
        self.assertEqual(self.resumes().list(), [])

    def test_deleting_a_non_default_keeps_the_default_and_removes_the_file(self):
        a, b = self.upload("A"), self.upload("B", DATA)
        self.assertEqual(len(list((self.root / "1").iterdir())), 2)
        self.assertTrue(self.resumes().delete(b["id"]))
        self.assertEqual(self.resumes().default()["id"], a["id"])
        self.assertEqual(len(list((self.root / "1").iterdir())), 1)
        self.assertFalse(self.resumes().delete(b["id"]))


# ── encryption at rest ────────────────────────────────────────────

class EncryptionTests(Base):
    def stored_path(self, v, uid=1):
        (p,) = list((self.root / str(uid)).glob("*.pdf.enc"))
        return p

    def test_the_file_on_disk_is_not_the_pdf_and_holds_none_of_its_words(self):
        v = self.upload("Backend")
        raw = self.stored_path(v).read_bytes()
        self.assertNotIn(b"%PDF", raw)
        self.assertNotIn(b"Postgres", raw)
        self.assertNotEqual(raw, BACKEND)
        self.assertTrue(raw.startswith(b"gAAAA"), "a Fernet token")

    def test_it_is_stored_under_data_resumes_user_id(self):
        v = ResumeStore(7, self.path).add("a.pdf", BACKEND, "Mine")           # the default root
        self.assertEqual(self.stored_path(v, 7).parent, self.dir / "resumes" / "7")

    def test_the_bytes_round_trip(self):
        v = self.upload("Backend")
        self.assertEqual(self.resumes().read(v["id"]), BACKEND)
        w = self.upload("Data", DATA)
        self.assertEqual(self.resumes().read(w["id"]), DATA)

    def test_the_extracted_text_round_trips_and_is_encrypted_in_the_database(self):
        v = self.upload("Backend")
        self.assertIn("Backend engineer Python Postgres", self.resumes().text(v["id"]))
        with contextlib.closing(sqlite3.connect(self.path)) as c:          # `with connect()` alone never closes it
            (cell,) = c.execute("SELECT text_enc FROM resume_variants").fetchone()
        self.assertTrue(cell.startswith("enc:"))
        self.assertNotIn("Postgres", cell)
        self.assertNotIn(b"Postgres", Path(self.path).read_bytes())

    def test_a_pdf_with_no_readable_text_is_still_stored(self):
        v = self.resumes().add("scan.pdf", b"%PDF-1.4\n% an image-only file", "Scan")
        self.assertEqual(self.resumes().text(v["id"]), "")
        self.assertEqual(self.resumes().read(v["id"]), b"%PDF-1.4\n% an image-only file")

    def test_a_wrong_key_is_an_error_not_the_ciphertext(self):
        v = self.upload("Backend")
        with mock.patch.object(crypto, "_fernet", Fernet(Fernet.generate_key())):
            with self.assertRaises(ResumeError) as cm:
                self.resumes().read(v["id"])
        self.assertIn("decrypt", str(cm.exception))
        self.assertEqual(self.resumes().read(v["id"]), BACKEND, "and with the right key it still works")

    def test_a_plaintext_pdf_in_the_store_is_refused(self):
        v = self.upload("Backend")
        self.stored_path(v).write_bytes(BACKEND)                  # someone drops the raw PDF there
        with self.assertRaises(ResumeError) as cm:
            self.resumes().read(v["id"])
        self.assertIn("not encrypted", str(cm.exception))

    def test_garbage_and_a_missing_file_are_refused(self):
        v = self.upload("Backend")
        self.stored_path(v).write_bytes(b"not a token")
        with self.assertRaises(ResumeError):
            self.resumes().read(v["id"])
        self.stored_path(v).unlink()
        with self.assertRaises(ResumeError) as cm:
            self.resumes().read(v["id"])
        self.assertIn("missing", str(cm.exception))

    def test_a_plaintext_temp_file_exists_only_inside_the_block(self):
        v = self.upload("Backend")
        source = self.resumes().source(v["id"])
        with source.materialize() as path:
            self.assertEqual(path.read_bytes(), BACKEND)
            self.assertEqual(path.name, "Backend.pdf")
            if os.name == "posix":                    # Windows has no permission bits to check
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            seen = path
        self.assertFalse(seen.exists())
        self.assertFalse(seen.parent.exists())

    def test_the_temp_file_is_removed_even_when_the_block_raises(self):
        source = self.resumes().source(self.upload("Backend")["id"])
        with self.assertRaises(RuntimeError):
            with source.materialize() as path:
                seen = path
                raise RuntimeError("upload blew up")
        self.assertFalse(seen.exists())

    def test_a_source_holds_no_plaintext_and_says_whether_it_can_be_read(self):
        v = self.upload("Backend")
        source = self.resumes().source(v["id"])
        self.assertNotIn(BACKEND, repr(vars(source)).encode())
        self.assertTrue(source.exists())
        self.stored_path(v).unlink()
        self.assertFalse(source.exists())
        with self.assertRaises(ResumeError):
            with source.materialize():
                pass

    def test_a_temp_file_left_by_a_killed_process_is_swept_up(self):
        tmp = self.dir / "tmpdir"
        tmp.mkdir()
        old, fresh, other = tmp / "apex-resume-old", tmp / "apex-resume-fresh", tmp / "something-else"
        for d in (old, fresh, other):
            d.mkdir()
            (d / "resume.pdf").write_bytes(BACKEND)
        long_ago = os.path.getmtime(old) - 7 * 3600
        os.utime(old, (long_ago, long_ago))
        with mock.patch("tempfile.gettempdir", lambda: str(tmp)):
            self.assertEqual(sweep_stale_temp(), 1)
        self.assertEqual((old.exists(), fresh.exists(), other.exists()), (False, True, True))

    def test_materializing_sweeps_stale_files_first(self):
        tmp = self.dir / "tmpdir"
        tmp.mkdir()
        stale = tmp / "apex-resume-stale"
        stale.mkdir()
        old = os.path.getmtime(stale) - 7 * 3600
        os.utime(stale, (old, old))
        source = self.resumes().source(self.upload("A")["id"])
        with mock.patch("tempfile.gettempdir", lambda: str(tmp)), source.materialize():
            self.assertFalse(stale.exists())

    def test_no_source_for_a_missing_resume(self):
        self.assertIsNone(self.resumes().source(999))


# ── isolation ─────────────────────────────────────────────────────

def s1_default(case):
    return case.resumes(1).default()["id"]


class IsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.a = self.upload("Backend", BACKEND, uid=1)
        self.b = self.upload("Backend", DATA, uid=2)                 # the same label is fine for another user

    def test_each_user_sees_only_their_own_resumes(self):
        self.assertEqual([v["id"] for v in self.resumes(1).list()], [self.a["id"]])
        self.assertEqual([v["id"] for v in self.resumes(2).list()], [self.b["id"]])
        self.assertEqual((self.a["is_default"], self.b["is_default"]), (True, True), "each user has their own default")

    def test_another_users_resume_cannot_be_read_changed_or_deleted(self):
        s2 = self.resumes(2)
        self.assertIsNone(s2.get(self.a["id"]))
        self.assertIsNone(s2.source(self.a["id"]))
        self.assertEqual(s2.text(self.a["id"]), "")
        with self.assertRaises(ResumeError) as cm:
            s2.read(self.a["id"])
        self.assertIn("no such resume", str(cm.exception), "refused as unknown, not merely as a missing file")
        self.assertIsNone(s2.set_default(self.a["id"]))
        self.assertEqual(s2.default()["id"], self.b["id"], "and Bob's own default was not disturbed by trying")
        self.assertEqual(s1_default(self), self.a["id"])
        self.assertFalse(s2.delete(self.a["id"]))
        self.assertEqual(self.resumes(1).read(self.a["id"]), BACKEND, "untouched")
        self.assertEqual(len(list((self.root / "1").iterdir())), 1)

    def test_deleting_a_users_default_does_not_move_anothers(self):
        c = self.upload("Second", MANAGER, uid=1)
        self.resumes(1).delete(self.a["id"])
        self.assertEqual(self.resumes(1).default()["id"], c["id"])
        self.assertEqual(self.resumes(2).default()["id"], self.b["id"])

    def test_the_limit_is_per_user(self):
        for i in range(MAX_VARIANTS - 1):
            self.upload(f"R{i}", uid=1)
        self.upload("Fine for Bob", uid=2)

    def test_files_live_in_separate_directories(self):
        self.assertEqual(len(list((self.root / "1").iterdir())), 1)
        self.assertEqual(len(list((self.root / "2").iterdir())), 1)

    def test_kits_are_per_user(self):
        job = self.job(uid=1)
        self.kit(job)
        self.assertIsNotNone(self.kit_store(1).current(job["job_id"]))
        self.assertIsNone(self.kit_store(2).current(job["job_id"]))
        self.assertEqual(self.kit_store(2).versions(job["job_id"]), [])
        self.assertIsNone(self.kit_store(2).get(job["job_id"], 1))

    def test_a_user_cannot_override_another_users_job_or_use_another_users_resume(self):
        job1 = self.job(uid=1)
        with self.assertRaises(ValueError):
            kits.override_resume(job1["job_id"], 2, self.b["id"], self.path, self.root)          # Alice's job
        mine = self.job(uid=2)
        with self.assertRaises(ValueError):
            kits.override_resume(mine["job_id"], 2, self.a["id"], self.path, self.root)          # Alice's resume
        self.assertIsNone(self.kit_store(1).current(job1["job_id"]))
        self.assertIsNone(self.kit_store(2).current(mine["job_id"]))

    def test_what_is_sent_to_the_llm_for_one_user_never_holds_another_users_resume(self):
        self.upload("Second", MANAGER, uid=1)
        self.llm.pick = {"variant_id": self.a["id"], "reason": "fits"}
        self.kit(self.job(uid=1))
        everything = " ".join(c[1] + c[2] for c in self.llm.calls)
        self.assertIn("Backend engineer Python Postgres", everything)
        self.assertNotIn("Data scientist", everything)

    def test_the_resume_a_filler_uploads_is_the_job_owners(self):
        job1, job2 = self.job(uid=1), self.job(uid=2)
        self.kit(job1)
        self.kit(job2, profile=None)
        s1, s2 = kits.resume_for(job1, self.path, self.root), kits.resume_for(job2, self.path, self.root)
        self.assertEqual((s1.store.user_id, s1.variant_id), (1, self.a["id"]))
        self.assertEqual((s2.store.user_id, s2.variant_id), (2, self.b["id"]))


# ── choosing the resume ───────────────────────────────────────────

class SelectionTests(Base):
    def chosen(self, job=None):
        kit = self.kit(job or self.job())
        return kit

    def setup_three(self):
        self.back = self.upload("Backend", BACKEND, tags="python,postgres")
        self.data = self.upload("Data", DATA, tags=["ml"])
        self.mgr = self.upload("Manager", MANAGER)

    def test_no_resumes_means_none_and_no_llm_selection(self):
        kit = self.chosen()
        self.assertEqual((kit["selection"], kit["variant_id"], kit["variant_label"]), ("none", None, ""))
        self.assertNotIn("select", self.llm.kinds())
        self.assertIsNone(kits.resume_for(self.job(), self.path, self.root))

    def test_a_single_resume_is_used_without_asking_the_llm(self):
        v = self.upload("Only")
        kit = self.chosen()
        self.assertEqual((kit["selection"], kit["variant_id"]), ("default", v["id"]))
        self.assertIn("only", kit["reason"])
        self.assertNotIn("select", self.llm.kinds())

    def test_the_llm_chooses_among_several(self):
        self.setup_three()
        self.llm.pick = {"variant_id": self.data["id"], "reason": "It mentions the ML stack."}
        kit = self.chosen()
        self.assertEqual((kit["selection"], kit["variant_id"], kit["variant_label"], kit["reason"]),
                         ("llm", self.data["id"], "Data", "It mentions the ML stack."))
        self.assertEqual(self.llm.kinds().count("select"), 1)

    def test_the_prompt_compares_the_job_with_every_resume(self):
        self.setup_three()
        self.llm.pick = {"variant_id": self.back["id"], "reason": "x"}
        self.chosen(self.job(title="Staff Backend Engineer"))
        (prompt,) = self.llm.prompts("select")
        for expected in ("Staff Backend Engineer", "Acme", DESCRIPTION, f"id={self.back['id']}", "label: Backend",
                         "tags: python, postgres", "Backend engineer Python Postgres Kafka", "label: Data",
                         "Data scientist PyTorch Spark", "label: Manager", "Engineering manager"):
            self.assertIn(expected, prompt)

    def test_an_id_given_as_a_digit_string_is_accepted(self):
        self.setup_three()
        self.llm.pick = {"variant_id": str(self.mgr["id"]), "reason": "x"}
        self.assertEqual(self.chosen()["variant_id"], self.mgr["id"])

    def test_a_long_reason_is_cut_and_tidied(self):
        self.setup_three()
        self.llm.pick = {"variant_id": self.back["id"], "reason": "word  " * 200}
        reason = self.chosen()["reason"]
        self.assertLessEqual(len(reason), kits.REASON_CHARS)
        self.assertNotIn("  ", reason)

    def test_an_llm_that_fails_falls_back_to_the_default(self):
        self.setup_three()
        self.resumes().set_default(self.mgr["id"])
        self.llm.pick = RuntimeError("LLM call failed (fake): key sk-SECRET-123 rejected")
        kit = self.chosen()
        self.assertEqual((kit["selection"], kit["variant_id"]), ("default", self.mgr["id"]))
        self.assertIn("failed", kit["reason"])
        self.assertNotIn("sk-SECRET", kit["reason"] + kit["error"], "exception text is never stored")

    def test_an_answer_that_is_not_one_of_the_users_resumes_falls_back_to_the_default(self):
        self.setup_three()
        other = self.upload("Bobs", MANAGER, uid=2)
        for bad in ({"variant_id": 99999, "reason": "x"}, {"variant_id": other["id"], "reason": "x"},
                    {"variant_id": True, "reason": "x"}, {"variant_id": None}, {"variant_id": "abc"},
                    {"variant_id": 1.5}, {}, [], "Backend", None, {"variant_id": [self.back["id"]]}):
            self.llm.pick = bad
            kit = self.kit(self.job(), reuse=False)
            self.assertEqual((kit["selection"], kit["variant_id"]), ("default", self.back["id"]), bad)

    def test_the_fallback_is_the_default_not_the_first(self):
        self.setup_three()
        self.resumes().set_default(self.data["id"])
        self.llm.pick = {"variant_id": 424242}
        self.assertEqual(self.chosen()["variant_id"], self.data["id"])

    def test_with_no_job_description_the_default_is_used_without_asking(self):
        self.setup_three()
        for description in ("", "   ", "too short"):
            kit = self.kit(self.job(description=description), reuse=False)
            self.assertEqual((kit["selection"], kit["variant_id"]), ("default", self.back["id"]))
            self.assertIn("description", kit["reason"])
        self.assertNotIn("select", self.llm.kinds())

    def test_parse_choice_is_strict(self):
        valid = [3, 4]
        self.assertEqual(kits.parse_choice({"variant_id": 3, "reason": " a  b "}, valid), (3, "a b"))
        self.assertEqual(kits.parse_choice({"variant_id": "4"}, valid), (4, ""))
        for bad in (None, [], "3", {"variant_id": 5}, {"variant_id": True}, {"variant_id": -3}, {"reason": "x"}):
            self.assertEqual(kits.parse_choice(bad, valid), (None, ""), bad)


# ── the kit ───────────────────────────────────────────────────────

class KitTests(Base):
    def test_a_ready_kit_has_the_resume_a_summary_and_a_cover_letter(self):
        v = self.upload("Backend")
        job = self.job()
        kit = self.kit(job)
        self.assertEqual((kit["status"], kit["version"], kit["variant_id"], kit["created_by"], kit["error"]),
                         ("ready", 1, v["id"], "agent", ""))
        self.assertEqual(kit["summary"], self.llm.summary)
        self.assertEqual(kit["cover_letter"], self.llm.letter)
        self.assertEqual(self.kit_store().current(job["job_id"])["version"], 1)

    def test_the_summary_is_three_sentences_whatever_the_llm_returns(self):
        self.upload("Backend")
        self.llm.summary = '"One is here. Two is here! Three is here? Four is too many. Five too."'
        self.assertEqual(self.kit(self.job())["summary"], "One is here. Two is here! Three is here?")
        self.assertEqual(kits.three_sentences("A.\n\n- B.   C. D."), "A. B. C.")
        self.assertEqual(kits.three_sentences("Dr. Rao leads. Then more."), "Dr. Rao leads. Then more.")
        for empty in ("", "   ", None, 5, '""'):
            self.assertEqual(kits.three_sentences(empty), "")

    def test_the_ghostwriter_writes_from_the_chosen_resume(self):
        self.upload("Backend")
        self.kit(self.job())
        for kind in ("summary", "letter"):
            (system,) = self.llm.prompts(kind)
            self.assertIn("RESUME BEING SENT", system)
            self.assertIn("Backend engineer Python Postgres Kafka", system)
            self.assertIn("Priya Sharma", system)
            self.assertIn(DESCRIPTION, system)

    def test_the_kit_is_put_on_the_jobs_timeline_without_its_text(self):
        self.upload("Backend")
        job = self.job()
        self.kit(job)
        events = JobsDB(path=self.path, user_id=1).get_application(job["job_id"])["events"]
        (e,) = [e for e in events if e["kind"] == "step" and e["detail"].get("step") == "kit_ready"]
        self.assertIn("v1", e["detail"]["message"])
        self.assertNotIn(self.llm.letter, str(e))
        self.assertNotIn(self.llm.summary, str(e))

    def test_an_llm_that_is_down_gives_a_kit_not_an_exception(self):
        v = self.upload("Backend")
        self.llm.text_fails = True
        job = self.job()
        kit = self.kit(job)
        self.assertEqual((kit["status"], kit["variant_id"], kit["summary"], kit["cover_letter"]),
                         ("partial", v["id"], "", ""))
        self.assertIn("summary", kit["error"])
        self.assertIn("cover letter", kit["error"])
        self.assertNotIn("sk-SECRET", str(kit))
        self.assertIsNotNone(kits.resume_for(job, self.path, self.root), "the application still has its resume")

    def test_with_nothing_at_all_the_kit_is_failed(self):
        self.llm.text_fails = True
        self.assertEqual(self.kit(self.job())["status"], "failed")

    def test_one_part_missing_is_partial(self):
        self.upload("Backend")
        self.llm.summary = ""
        kit = self.kit(self.job())
        self.assertEqual((kit["status"], kit["cover_letter"] != ""), ("partial", True))
        self.assertIn("summary", kit["error"])

    def test_without_a_profile_the_resume_is_still_chosen(self):
        v = self.upload("Backend")
        kit = self.kit(self.job(), profile=None)
        self.assertEqual((kit["status"], kit["variant_id"], kit["summary"]), ("partial", v["id"], ""))
        self.assertIn("profile", kit["error"])
        self.assertEqual([k for k in self.llm.kinds() if k != "select"], [])

    def test_no_resumes_but_a_profile_still_gets_text_and_status_ready(self):
        kit = self.kit(self.job())
        self.assertEqual((kit["status"], kit["selection"], kit["summary"] != "", kit["cover_letter"] != ""),
                         ("ready", "none", True, True))

    def test_a_ready_kit_is_reused_so_a_restart_costs_nothing(self):
        self.upload("Backend")
        job = self.job()
        first = self.kit(job)
        calls = len(self.llm.calls)
        again = self.kit(job)
        self.assertEqual((again["version"], len(self.llm.calls)), (first["version"], calls))

    def test_a_kit_that_is_not_ready_is_made_again_as_a_new_version(self):
        self.upload("Backend")
        job = self.job()
        self.llm.text_fails = True
        self.assertEqual(self.kit(job)["version"], 1)
        self.llm.text_fails = False
        second = self.kit(job)
        self.assertEqual((second["version"], second["status"]), (2, "ready"))

    def test_every_change_is_a_new_version_and_history_is_kept(self):
        self.upload("Backend")
        job = self.job()
        first_summary = self.llm.summary
        self.kit(job)
        self.kit(job, reuse=False)
        self.llm.summary = "A new first. A new second. A new third."
        third = self.kit(job, reuse=False)
        store = self.kit_store()
        self.assertEqual([k["version"] for k in store.versions(job["job_id"])], [3, 2, 1])
        self.assertEqual(store.current(job["job_id"])["summary"], third["summary"])
        self.assertEqual(store.get(job["job_id"], 1)["summary"], first_summary)

    def test_a_kit_never_raises_even_when_the_stores_break(self):
        self.upload("Backend")
        with mock.patch.object(kits, "KitStore", side_effect=RuntimeError("db gone")), \
             self.assertLogs("applier.kit", "ERROR"):
            self.assertIsNone(self.kit(self.job()))

    def test_a_kit_never_raises_when_the_ghostwriter_does(self):
        self.upload("Backend")
        with mock.patch("intelligence.ghostwriter.tailored_summary", side_effect=RuntimeError("boom")), \
             mock.patch("intelligence.ghostwriter.cover_letter", side_effect=RuntimeError("boom")):
            kit = self.kit(self.job())
        self.assertEqual((kit["status"], kit["summary"], kit["cover_letter"]), ("partial", "", ""))

    def test_unknown_kit_fields_are_refused_by_the_store(self):
        with self.assertRaises(ValueError):
            self.kit_store().add_version("j", surprise=1)
        with self.assertRaises(ValueError):
            self.kit_store().add_version("j", selection="maybe")
        with self.assertRaises(ValueError):
            KitStore(None, self.path)


class OverrideTests(Base):
    def setUp(self):
        super().setUp()
        self.back = self.upload("Backend", BACKEND)
        self.data = self.upload("Data", DATA)
        self.llm.pick = {"variant_id": self.back["id"], "reason": "fits"}
        self.job_ = self.job()
        self.first = self.kit(self.job_)

    def override(self, vid):
        return kits.override_resume(self.job_["job_id"], 1, vid, self.path, self.root)

    def test_the_person_can_choose_another_resume(self):
        kit = self.override(self.data["id"])
        self.assertEqual((kit["variant_id"], kit["variant_label"], kit["selection"], kit["created_by"], kit["version"]),
                         (self.data["id"], "Data", "override", "user", 2))
        self.assertEqual(self.kit_store().current(self.job_["job_id"])["variant_id"], self.data["id"])

    def test_the_summary_and_letter_are_carried_over_and_the_first_version_is_kept(self):
        kit = self.override(self.data["id"])
        self.assertEqual((kit["summary"], kit["cover_letter"], kit["status"]),
                         (self.first["summary"], self.first["cover_letter"], "ready"))
        self.assertEqual(self.kit_store().get(self.job_["job_id"], 1)["variant_id"], self.back["id"])

    def test_the_fillers_then_upload_the_chosen_resume(self):
        self.assertEqual(kits.resume_for(self.job_, self.path, self.root).variant_id, self.back["id"])
        self.override(self.data["id"])
        self.assertEqual(kits.resume_for(self.job_, self.path, self.root).variant_id, self.data["id"])

    def test_a_regeneration_keeps_the_override(self):
        self.override(self.data["id"])
        self.llm.calls.clear()
        kit = self.kit(self.job_, reuse=False)
        self.assertEqual((kit["variant_id"], kit["selection"], kit["version"]), (self.data["id"], "override", 3))
        self.assertNotIn("select", self.llm.kinds(), "the person's choice is not second-guessed")

    def test_reselect_lets_the_selection_decide_again(self):
        self.override(self.data["id"])
        kit = self.kit(self.job_, reuse=False, reselect=True)
        self.assertEqual((kit["variant_id"], kit["selection"]), (self.back["id"], "llm"))

    def test_an_override_whose_resume_was_deleted_is_dropped(self):
        self.override(self.data["id"])
        self.resumes().delete(self.data["id"])
        kit = self.kit(self.job_, reuse=False)
        self.assertEqual((kit["selection"], kit["variant_id"]), ("default", self.back["id"]))

    def test_the_filler_falls_back_to_the_default_when_the_kits_resume_is_gone(self):
        self.override(self.data["id"])
        self.resumes().delete(self.data["id"])
        self.assertEqual(kits.resume_for(self.job_, self.path, self.root).variant_id, self.back["id"])

    def test_choosing_before_a_kit_exists_makes_a_partial_kit_a_later_one_completes(self):
        job = self.job()
        kit = kits.override_resume(job["job_id"], 1, self.data["id"], self.path, self.root)
        self.assertEqual((kit["status"], kit["selection"]), ("partial", "override"))
        later = self.kit(job)
        self.assertEqual((later["variant_id"], later["selection"], later["status"], later["version"]),
                         (self.data["id"], "override", "ready", 2))

    def test_the_override_is_on_the_timeline(self):
        self.override(self.data["id"])
        events = JobsDB(path=self.path, user_id=1).get_application(self.job_["job_id"])["events"]
        self.assertTrue([e for e in events if e["detail"].get("selection") == "override"])

    def test_nothing_that_is_not_a_job_or_a_resume_can_be_chosen(self):
        with self.assertRaises(ValueError):
            kits.override_resume("no-such-job", 1, self.data["id"], self.path, self.root)
        with self.assertRaises(ValueError):
            self.override(424242)
        self.assertEqual(self.kit_store().current(self.job_["job_id"])["version"], 1)


class ResumeForTests(Base):
    def test_no_kit_means_the_users_default_resume(self):
        a, b = self.upload("A"), self.upload("B", DATA)
        self.resumes().set_default(b["id"])
        self.assertEqual(kits.resume_for(self.job(), self.path, self.root).variant_id, b["id"])

    def test_no_resumes_means_none_so_the_old_resume_file_is_used(self):
        self.assertIsNone(kits.resume_for(self.job(), self.path, self.root))

    def test_it_never_raises(self):
        with mock.patch.object(kits, "ResumeStore", side_effect=RuntimeError("x")), self.assertLogs("applier.kit", "ERROR"):
            self.assertIsNone(kits.resume_for(self.job(), self.path, self.root))


# ── the fillers upload the chosen resume ──────────────────────────

class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = cls._browser = None
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch()
        except Exception as e:
            cls._stop()
            raise unittest.SkipTest(f"no usable Chromium for Playwright ({type(e).__name__})")

    @classmethod
    def _stop(cls):
        for closer in (getattr(cls._browser, "close", None), getattr(cls._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        cls._pw = cls._browser = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()


class FillerUploadTests(BrowserCase, Base):
    """The fillers take a plain path. The browser reads an uploaded file from disk when the
    form is SENT, so the decrypted copy must live for the whole pass (the Orchestrator's job),
    not just the set_input_files call: these tests upload from inside materialize() and read
    the file back through the page."""

    def setUp(self):
        Base.setUp(self)
        self.page = self._browser.new_page()
        self.addCleanup(self.page.close)
        self.legacy = self.dir / "resume.pdf"
        self.legacy.write_bytes(make_pdf("The old single resume"))
        self.source = self.resumes().source(self.upload("Data", DATA)["id"])

    def field(self):
        from applier.form_analyzer import FormField
        return FormField(label="Resume", field_key="resume", selector="#cv", field_type="file", options=[],
                         is_required=True, db_value="", needs_manual=False, placeholder="")

    def uploaded(self, selector="input[type=file]"):
        return self.page.evaluate("""async (sel) => { const f = document.querySelector(sel).files[0];
            return f ? {name: f.name, size: f.size, head: new TextDecoder().decode((await f.arrayBuffer()).slice(0, 5))} : null; }""", selector)

    def test_the_universal_filler_uploads_the_chosen_resume_and_the_browser_can_read_it(self):
        from applier import form_filler
        self.page.set_content('<label>Resume<input type="file" id="cv"></label>')
        with mock.patch.object(form_filler, "_delay", lambda *a: None), self.source.materialize() as path:
            self.assertTrue(form_filler._fill_one(self.page, self.field(), path))
            self.assertEqual(self.uploaded(), {"name": "Data.pdf", "size": len(DATA), "head": "%PDF-"})
            seen = path
        self.assertFalse(seen.exists(), "the plaintext copy is gone when the pass ends")

    def test_execute_passes_the_chosen_resume_through(self):
        from applier import form_filler
        from applier.form_analyzer import FormAnalysis
        self.page.set_content('<label>Resume<input type="file" id="cv"></label>')
        with mock.patch.object(form_filler, "_delay", lambda *a: None), self.source.materialize() as path:
            filled, failed = form_filler.execute(self.page, FormAnalysis(page_url="u", job_id="j", fields=[self.field()]),
                                                 resume_path=path)
            self.assertEqual((len(filled), len(failed)), (1, 0))
            self.assertEqual(self.uploaded()["size"], len(DATA))

    def test_without_a_chosen_resume_the_old_file_is_uploaded_as_before(self):
        from applier import form_filler
        self.page.set_content('<input type="file" id="cv">')
        with mock.patch.object(form_filler, "_delay", lambda *a: None), mock.patch.object(cfg, "RESUME_PATH", self.legacy):
            self.assertTrue(form_filler._fill_one(self.page, self.field(), None))
        self.assertEqual(self.uploaded()["name"], "resume.pdf")
        self.assertTrue(self.legacy.exists())

    def test_the_adapters_upload_the_chosen_resume_and_the_browser_can_read_it(self):
        from applier.adapters import standard, form as adapter_form
        from applier.adapters.greenhouse import GreenhouseAdapter
        html = (Path(__file__).resolve().parent / "fixtures" / "greenhouse_application.html").read_text(encoding="utf-8")
        self.page.set_content(html)
        adapter = GreenhouseAdapter()
        fields = adapter.extract_fields(self.page)
        standard.resolve(fields, {"_flat": {"first_name": "P", "last_name": "S", "email": "p@x.io"}})
        with mock.patch.object(adapter_form, "PAUSE", (0, 0)), self.source.materialize() as path:
            filled, failed = adapter.fill(self.page, fields, path)
            self.assertIn("resume_file", [f.field_key for f in filled])
            self.assertEqual(self.uploaded("#resume"), {"name": "Data.pdf", "size": len(DATA), "head": "%PDF-"})
            seen = path
        self.assertFalse(seen.exists())

    def test_the_adapter_without_a_chosen_resume_uses_the_old_file(self):
        from applier.adapters import standard, form as adapter_form
        from applier.adapters.greenhouse import GreenhouseAdapter
        html = (Path(__file__).resolve().parent / "fixtures" / "greenhouse_application.html").read_text(encoding="utf-8")
        self.page.set_content(html)
        adapter = GreenhouseAdapter()
        fields = adapter.extract_fields(self.page)
        standard.resolve(fields, {"_flat": {}})
        with mock.patch.object(adapter_form, "PAUSE", (0, 0)), mock.patch.object(cfg, "RESUME_PATH", self.legacy):
            adapter.fill(self.page, fields, None)
        self.assertEqual(self.uploaded("#resume")["name"], "resume.pdf")

    def test_the_adapter_runner_hands_the_chosen_resume_to_the_adapter(self):
        from applier.adapters import runner
        adapter = mock.Mock()
        adapter.start_url = lambda u: u
        adapter.fill.return_value = ([], [])
        analysis = mock.Mock(missing=[], fields=[], fillable=[])
        marker = self.dir / "chosen.pdf"
        with mock.patch.object(runner, "_read", return_value=(analysis, [])), \
             mock.patch.object(runner, "_open", return_value=None), \
             mock.patch.object(runner.U, "_report_page"), mock.patch.object(runner.U, "_screenshot", return_value=""), \
             mock.patch.object(runner.U, "_report_filled"), mock.patch.object(runner, "_snapshot", return_value=[]), \
             mock.patch.object(runner.U, "_page_url", return_value="u"):
            runner.run_application(adapter, self.page, {"job_id": "j", "url": "u"}, {}, resume_path=marker)
            self.assertEqual(adapter.fill.call_args[0][2], marker)
            runner.run_application(adapter, self.page, {"job_id": "j", "url": "u"}, {})
            self.assertEqual(adapter.fill.call_args[0][2], cfg.RESUME_PATH)


class UniversalPlumbingTests(Base):
    """universal.run_application / run_iteration2 / run_submit hand the chosen resume to execute()."""

    class FakePage:
        url = "https://example.com/apply"
        def goto(self, *a, **k): pass
        def inner_text(self, sel): return "Step 1 of 1"
        def evaluate(self, js): return []

    def setUp(self):
        super().setUp()
        from applier import universal
        self.universal = universal
        self.seen = []

        class A:
            fields, missing, fillable = [], [], []
            has_file_upload = False
            submit_selector = ""
            page_url = "u"
            def __init__(s, *a, **k): pass

        def execute(page, analysis, **k):
            self.seen.append(k.get("resume_path", "<absent>"))
            return [], []
        for target, repl in [("detect_captcha", lambda p: False), ("is_login_wall", lambda p: False),
                             ("_page_has_form", lambda p: True), ("_wait_for_form", lambda p, *a: None),
                             ("extract_fields", lambda p: "form text"), ("analyze", lambda **k: A()),
                             ("execute", execute), ("click_next", lambda p, a: False), ("click_submit", lambda p, a: True),
                             ("_screenshot", lambda *a: ""), ("_is_review_page", lambda p: False),
                             ("page_text_from_extraction", lambda t: t), ("_validation_errors", lambda p: [])]:
            p = mock.patch.object(universal, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("applier.universal.time.sleep", lambda s: None),):
            p.start()
            self.addCleanup(p.stop)

    JOB = {"job_id": "j1", "url": "https://example.com/job", "title": "Dev", "company": "Acme", "source": "linkedin"}

    def test_every_pass_uploads_the_resume_it_is_given(self):
        marker = object()
        self.universal.run_application(self.FakePage(), self.JOB, {}, resume_path=marker)
        self.universal.run_iteration2(self.FakePage(), self.JOB, {}, ["k"], resume_path=marker)
        self.universal.run_submit(self.FakePage(), self.JOB, {}, resume_path=marker)
        self.assertEqual(self.seen, [marker, marker, marker])

    def test_without_one_the_old_resume_file_is_used(self):
        self.universal.run_application(self.FakePage(), self.JOB, {})
        self.universal.run_iteration2(self.FakePage(), self.JOB, {}, ["k"])
        self.universal.run_submit(self.FakePage(), self.JOB, {})
        self.assertEqual(self.seen, [cfg.RESUME_PATH] * 3)


# ── the Orchestrator ──────────────────────────────────────────────

class Playwright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class QuietSettings:
    def read(self, key, default=""):
        return {"quality_first": "false", "daily_apply_limit": "0", "min_delay_between_applies": "0"}.get(key, default)


class OrchestratorTests(Base):
    def setUp(self):
        super().setUp()
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.addCleanup(lambda: setattr(cfg, "SESSIONS_DIR", self._sessions))
        self.resume_kwargs = []                   # what each filler pass saw: (path exists during the pass, its bytes), or "<absent>"
        self.resume_paths = []
        self.order = []                           # "kit" / "filler"
        ok = {"success": True, "screenshots": [], "missing_fields": [], "error": "", "needs_review": True,
              "review": {"fields": [], "url": "u"}, "dry_run": False}

        def filler(name):
            def run(page, job, profile, *a, **kw):
                self.order.append("filler")
                given = kw.get("resume_path")
                if given is None:
                    self.resume_kwargs.append("<absent>")
                else:
                    self.resume_paths.append(given)
                    self.resume_kwargs.append(Path(given).read_bytes() if Path(given).exists() else "<missing>")
                return dict(ok)
            return run
        real_prepare = kits.prepare_kit

        def prepare(*a, **kw):
            self.order.append("kit")
            return real_prepare(*a, **kw)
        patches = [mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: QuietSettings())),
                   mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)),
                   mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                   mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                   mock.patch("applier.adapters.adapter_for", lambda *a, **k: None),
                   mock.patch("applier.kit.prepare_kit", prepare)]
        for name in ("run_application", "run_iteration2", "run_submit"):
            patches.append(mock.patch(f"applier.universal.{name}", filler(name)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.profile = mock.Mock(summary=lambda: PROFILE, get_pending_missing=lambda job_id: [])

    def orchestrator(self, job):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = job["job_id"], JobsDB(path=self.path, user_id=job["user_id"]).get_by_id(job["job_id"])
        o.profile_db = self.profile
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(job["job_id"]) or ApplicationSession(
            job_id=job["job_id"], job_url="u", job_title="t", company="Acme", source="linkedin")
        o._open_browser = lambda pw: Ctx()
        return o

    def stage(self, job, uid=1):
        return JobsDB(path=self.path, user_id=uid).get_stage(job["job_id"])

    def test_the_kit_is_prepared_during_preparing_before_the_form_is_touched(self):
        self.upload("Backend")
        job = self.job()
        self.orchestrator(job).run_iter1()
        self.assertEqual(self.order[:2], ["kit", "filler"])
        kit = self.kit_store().current(job["job_id"])
        self.assertEqual((kit["status"], kit["created_by"]), ("ready", "agent"))
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_the_form_code_is_given_the_kits_resume(self):
        a, b = self.upload("Backend"), self.upload("Data", DATA)
        self.llm.pick = {"variant_id": b["id"], "reason": "x"}
        job = self.job()
        self.orchestrator(job).run_iter1()
        self.assertEqual(self.resume_kwargs, [DATA], "the filler saw the chosen resume's real bytes, decrypted")
        (path,) = self.resume_paths
        self.assertEqual(Path(path).name, "Data.pdf")

    def test_the_decrypted_copy_exists_for_the_pass_and_is_gone_after_it(self):
        self.upload("Backend")
        job = self.job()
        self.orchestrator(job).run_iter1()
        (path,) = self.resume_paths
        self.assertFalse(Path(path).exists())
        self.assertFalse(Path(path).parent.exists())
        self.orchestrator(job).submit()
        self.assertEqual(len(self.resume_paths), 2)
        self.assertTrue(all(not Path(p).exists() for p in self.resume_paths))

    def test_the_decrypted_copy_is_removed_even_when_the_pass_fails(self):
        self.upload("Backend")
        job = self.job()
        with mock.patch("applier.universal.run_application", side_effect=RuntimeError("form exploded")):
            self.orchestrator(job).run_iter1()
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("apex-resume-*")), [])

    def test_a_resume_that_cannot_be_decrypted_is_on_the_timeline_and_the_application_goes_on(self):
        self.upload("Backend")
        job = self.job()
        with mock.patch.object(crypto, "_fernet", Fernet(Fernet.generate_key())), self.assertLogs("applier.session", "ERROR"):
            self.orchestrator(job).run_iter1()
        self.assertEqual(self.resume_kwargs, ["<absent>"], "the filler falls back to the single resume file")
        self.assertEqual(self.stage(job), "ready_for_review")
        events = JobsDB(path=self.path, user_id=1).get_application(job["job_id"])["events"]
        self.assertTrue([e for e in events if e["detail"].get("step") == "resume_unavailable"])

    def test_a_person_with_no_resumes_uploads_the_old_resume_file_as_before(self):
        job = self.job()
        self.orchestrator(job).run_iter1()
        self.assertEqual(self.resume_kwargs, ["<absent>"])
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_a_kit_that_blows_up_never_blocks_the_application(self):
        self.upload("Backend")
        job = self.job()
        with mock.patch("applier.kit.prepare_kit", side_effect=RuntimeError("kit exploded")), \
             self.assertLogs("applier.session", "ERROR"):
            self.orchestrator(job).run_iter1()
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertEqual(self.resume_kwargs, [BACKEND], "and it still uploads the default resume")

    def test_an_llm_that_is_down_never_blocks_the_application(self):
        self.upload("Backend")
        self.upload("Data", DATA)
        self.llm.text_fails = True
        self.llm.pick = RuntimeError("down")
        job = self.job()
        self.orchestrator(job).run_iter1()
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertEqual(self.kit_store().current(job["job_id"])["status"], "partial")

    def test_an_override_made_after_the_first_pass_is_what_the_submit_uploads(self):
        a, b = self.upload("Backend"), self.upload("Data", DATA)
        self.llm.pick = {"variant_id": a["id"], "reason": "x"}
        job = self.job()
        self.orchestrator(job).run_iter1()
        kits.override_resume(job["job_id"], 1, b["id"], self.path, self.root)
        self.orchestrator(job).submit()
        self.assertEqual(self.resume_kwargs, [BACKEND, DATA])

    def test_a_restarted_application_keeps_its_kit(self):
        self.upload("Backend")
        job = self.job()
        self.orchestrator(job).run_iter1()
        JobsDB(path=self.path, user_id=1).transition(job["job_id"], "preparing")
        calls = len(self.llm.calls)
        self.orchestrator(job).run_iter1()
        self.assertEqual((len(self.llm.calls), self.kit_store().current(job["job_id"])["version"]), (calls, 1))

    def test_only_the_profiles_owner_gets_text_written_from_it(self):
        self.upload("Backend", uid=2)
        job = self.job(uid=2)
        self.orchestrator(job).run_iter1()
        kit = self.kit_store(2).current(job["job_id"])
        self.assertEqual((kit["summary"], kit["status"]), ("", "partial"))
        self.assertEqual([k for k in self.llm.kinds() if k != "select"], [])


# ── over HTTP ─────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class KitHttpTests(unittest.TestCase):
    def setUp(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.llm = FakeLLM()
        self._patches = [mock.patch("core.llm.chat", self.llm.chat), mock.patch("core.llm.chat_json", self.llm.chat_json)]
        for p in self._patches:
            p.start()
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.DATA_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.DATA_DIR = tmp / "jobs.db", tmp / "profile.db", tmp
        cfg.SESSIONS_DIR = tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance = UsersDB(path=tmp / "users.db")
        auth._serializer = None
        self._dev = os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)           # alice = owner
        self.client.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})
        self.anon = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB
        for p in self._patches:
            p.stop()
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.DATA_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        if self._dev is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._dev
        self.tmp.cleanup()

    def up(self, client, label="Backend", pdf=BACKEND, **form):
        return client.post("/api/resumes", files={"file": ("r.pdf", pdf, "application/pdf")},
                           data={"label": label, **form})

    def test_everything_needs_a_login(self):
        for method, url in (("get", "/api/resumes"), ("post", "/api/resumes"), ("put", "/api/resumes/1/default"),
                            ("delete", "/api/resumes/1"), ("get", "/api/kits/x"), ("put", "/api/kits/x/resume"),
                            ("post", "/api/kits/x/regenerate")):
            kw = {"json": {}} if method in ("put", "post") and "resumes" not in url else {}
            self.assertEqual(getattr(self.anon, method)(url, **kw).status_code, 401, url)

    def test_upload_list_default_delete(self):
        r = self.up(self.client, tags="python, go")
        self.assertEqual(r.status_code, 200)
        v = r.json()
        self.assertEqual((v["label"], v["tags"], v["is_default"]), ("Backend", ["python", "go"], True))
        self.assertNotIn("stored_name", v)
        w = self.up(self.client, "Data", DATA).json()
        self.assertEqual([x["is_default"] for x in self.client.get("/api/resumes").json()["resumes"]], [True, False])
        self.assertEqual(self.client.put(f"/api/resumes/{w['id']}/default").json()["is_default"], True)
        self.assertEqual(self.client.delete(f"/api/resumes/{w['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/resumes/{w['id']}").status_code, 404)

    def test_validation_errors_are_400_and_413(self):
        self.assertEqual(self.up(self.client, pdf=b"not a pdf").status_code, 400)
        self.assertEqual(self.up(self.client, label="").status_code in (400, 422), True)
        big = b"%PDF-1.4\n" + b"0" * (MAX_BYTES + 10)
        self.assertEqual(self.up(self.client, "Big", big).status_code, 413)
        self.assertEqual(self.client.get("/api/resumes").json()["resumes"], [])

    def test_another_users_resumes_are_not_visible_or_changeable(self):
        v = self.up(self.client).json()
        self.assertEqual(self.bob.get("/api/resumes").json()["resumes"], [])
        self.assertEqual(self.bob.put(f"/api/resumes/{v['id']}/default").status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/resumes/{v['id']}").status_code, 404)
        self.assertEqual(len(self.client.get("/api/resumes").json()["resumes"]), 1)

    def test_a_kit_is_readable_by_its_owner_only_and_overridable_by_the_owner_only(self):
        a, b = self.up(self.client).json(), self.up(self.client, "Data", DATA).json()
        job = JobsDB(user_id=1)
        jid = job.add({"url": "https://example.com/1", "title": "Dev", "company": "Acme", "source": "linkedin",
                       "description": DESCRIPTION})
        r = self.client.put(f"/api/kits/{jid}/resume", json={"variant_id": b["id"]})
        self.assertEqual((r.status_code, r.json()["selection"], r.json()["variant_id"]), (200, "override", b["id"]))
        got = self.client.get(f"/api/kits/{jid}").json()
        self.assertEqual((got["current"]["version"], len(got["versions"])), (1, 1))
        self.assertEqual(self.bob.get(f"/api/kits/{jid}").status_code, 404)
        self.assertEqual(self.bob.put(f"/api/kits/{jid}/resume", json={"variant_id": b["id"]}).status_code, 403)
        self.assertEqual(self.bob.post(f"/api/kits/{jid}/regenerate", json={}).status_code, 403)
        self.assertEqual(self.client.put(f"/api/kits/{jid}/resume", json={"variant_id": "x"}).status_code, 400)
        self.assertEqual(self.client.put(f"/api/kits/{jid}/resume", json={"variant_id": 9999}).status_code, 404)
        self.assertEqual(self.client.get("/api/kits/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
