# db/resumes_db.py
"""
Resume variants: several PDFs per user (say "Backend", "Data", "Management"),
each with a label and tags, exactly one the default. The agent picks the best
one per application (applier/kit.py) and the form fillers upload it.

STORAGE. The PDF itself is encrypted with the app's own helper
(core.crypto.encrypt_bytes) and written to data/resumes/<user_id>/<random>.pdf.enc;
the file name on disk is random, never the uploaded name. The metadata lives in
the jobs database (table resume_variants), every query scoped by user_id, with
no unscoped view. The text pulled out of the PDF (what the LLM compares with
the job description) is encrypted too (core.crypto.encrypt) - an encrypted
file next to a plaintext copy of its contents would protect nothing.

PLAINTEXT only ever exists in memory, and in a temp file while a form is being
filled: ResumeSource.materialize() writes it to a private temp directory
(0700) for the length of a `with` block and deletes it afterwards, even if
the block raises; a temp file an earlier process left behind (killed
mid-pass) is swept up the next time. The Orchestrator wraps each filler pass in
it and hands the fillers the plain path.

UPLOAD CHECKS. At most MAX_BYTES (5 MB); it must be a real PDF - the bytes
start with the %PDF- signature, whatever the file is called; a label (unique
per user); at most 10 variants per user (the selection prompt carries every
one). The first variant becomes the default. Deleting the default promotes the
oldest remaining one, so a user who has any resume has a default.

Reading is strict: a stored file that is not an encrypted token (a plaintext
PDF someone dropped there) or that will not decrypt (wrong key) is refused, not
passed through - core.crypto.decrypt_bytes alone would hand it back as it is.
"""

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core import crypto
from core.config import cfg

logger = logging.getLogger(__name__)

MAX_BYTES      = 5 * 1024 * 1024
MAX_VARIANTS   = 10
MAX_LABEL      = 60
MAX_TAGS       = 10
MAX_TAG        = 30
TEXT_CHARS     = 6000          # how much of a resume's text is kept for the LLM
TEXT_PAGES     = 4
PDF_SIGNATURE  = b"%PDF-"

RESUME_DDL = (
    """CREATE TABLE IF NOT EXISTS resume_variants (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        label       TEXT NOT NULL,
        tags        TEXT NOT NULL DEFAULT '[]',
        is_default  INTEGER NOT NULL DEFAULT 0,
        filename    TEXT NOT NULL DEFAULT 'resume.pdf',
        stored_name TEXT NOT NULL,
        size        INTEGER NOT NULL,
        text_enc    TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_resume_variants_user ON resume_variants(user_id, id)",
)


class ResumeError(ValueError):
    """An upload or a read that is not acceptable; the text says why. `status`
    is the HTTP status a route should answer with."""
    status = 400


class ResumeTooLarge(ResumeError):
    status = 413


# ── validation ────────────────────────────────────────────────────

def check_pdf(data: bytes) -> None:
    """ResumeError unless `data` is a non-empty PDF within the size limit."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ResumeError("the file is empty")
    if len(data) > MAX_BYTES:
        raise ResumeTooLarge(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not bytes(data[:len(PDF_SIGNATURE)]) == PDF_SIGNATURE:
        raise ResumeError("the file is not a PDF (it does not start with the PDF signature)")


def clean_label(label) -> str:
    text = re.sub(r"\s+", " ", label).strip() if isinstance(label, str) else ""
    if not text:
        raise ResumeError("a label is required")
    if len(text) > MAX_LABEL:
        raise ResumeError(f"the label is longer than {MAX_LABEL} characters")
    return text


def clean_tags(tags) -> list[str]:
    """A list (or a comma-separated string) of short tags, lowercased, no duplicates."""
    if tags is None or tags == "":
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)) or not all(isinstance(t, str) for t in tags):
        raise ResumeError("tags must be a list of words")
    out: list[str] = []
    for t in tags:
        t = re.sub(r"\s+", " ", t).strip().lower()
        if not t:
            continue
        if len(t) > MAX_TAG:
            raise ResumeError(f"a tag is longer than {MAX_TAG} characters")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ResumeError(f"at most {MAX_TAGS} tags")
    return out


def safe_filename(name) -> str:
    """The uploaded name reduced to something safe to put on a file again."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .") or "resume.pdf"
    return (base if base.lower().endswith(".pdf") else base + ".pdf")[:100]


def extract_text(data: bytes) -> str:
    """The text of the first pages of a PDF, for the LLM to compare. "" if
    there is none (a scanned image) or the PDF will not parse - never raises."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages[:TEXT_PAGES]]
        return re.sub(r"[ \t]+", " ", "\n".join(pages)).strip()[:TEXT_CHARS]
    except Exception as e:
        logger.debug(f"[Resumes] no text extracted: {e}")
        return ""


def _public(row) -> dict:
    """A variant as the API shows it: no disk name, no text."""
    return {"id": row["id"], "label": row["label"], "tags": json.loads(row["tags"]),
            "is_default": bool(row["is_default"]), "filename": row["filename"],
            "size": row["size"], "created_at": row["created_at"]}


# ── the store ─────────────────────────────────────────────────────

class ResumeStore:
    def __init__(self, user_id: int, path: Path | None = None, root: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("ResumeStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dir = (root or (cfg.DATA_DIR / "resumes")) / str(user_id)
        with self._conn() as c:
            for ddl in RESUME_DDL:
                c.execute(ddl)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── writing ───────────────────────────────────────────────

    def add(self, filename: str, data: bytes, label, tags=None, make_default: bool = False) -> dict:
        """Validate and store an upload; returns the variant. ResumeError (413
        for ResumeTooLarge) if it is not acceptable - nothing is written then."""
        check_pdf(data)
        label, tags = clean_label(label), clean_tags(tags)
        with self._conn() as c:
            rows = c.execute("SELECT label FROM resume_variants WHERE user_id=?", (self.user_id,)).fetchall()
        if len(rows) >= MAX_VARIANTS:
            raise ResumeError(f"you already have {MAX_VARIANTS} resumes; delete one first")
        if any(r["label"].casefold() == label.casefold() for r in rows):
            raise ResumeError(f"you already have a resume labelled {label!r}")

        stored = uuid.uuid4().hex + ".pdf.enc"
        text = crypto.encrypt(extract_text(bytes(data)))
        self.dir.mkdir(parents=True, exist_ok=True)
        target, tmp = self.dir / stored, self.dir / (stored + ".part")
        tmp.write_bytes(crypto.encrypt_bytes(bytes(data)))
        os.replace(tmp, target)
        try:
            with self._conn() as c:
                default = 1 if (make_default or not rows) else 0
                if default:
                    c.execute("UPDATE resume_variants SET is_default=0 WHERE user_id=?", (self.user_id,))
                cur = c.execute(
                    "INSERT INTO resume_variants (user_id, label, tags, is_default, filename, stored_name, "
                    "size, text_enc, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.user_id, label, json.dumps(tags), default, safe_filename(filename), stored,
                     len(data), text, datetime.now().isoformat()))
                new_id = cur.lastrowid
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.get(new_id)

    def set_default(self, variant_id: int) -> dict | None:
        """Make this the (only) default. None if it is not one of this user's."""
        with self._conn() as c:
            if not c.execute("SELECT 1 FROM resume_variants WHERE id=? AND user_id=?",
                             (variant_id, self.user_id)).fetchone():
                return None
            c.execute("UPDATE resume_variants SET is_default=(id=?) WHERE user_id=?", (variant_id, self.user_id))
        return self.get(variant_id)

    def delete(self, variant_id: int) -> bool:
        """Delete a variant and its encrypted file. Deleting the default makes
        the oldest remaining resume the default."""
        with self._conn() as c:
            row = c.execute("SELECT stored_name, is_default FROM resume_variants WHERE id=? AND user_id=?",
                            (variant_id, self.user_id)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM resume_variants WHERE id=? AND user_id=?", (variant_id, self.user_id))
            if row["is_default"]:
                c.execute("UPDATE resume_variants SET is_default=1 WHERE id=("
                          "SELECT MIN(id) FROM resume_variants WHERE user_id=?) AND user_id=?",
                          (self.user_id, self.user_id))
        (self.dir / row["stored_name"]).unlink(missing_ok=True)
        return True

    # ── reading ───────────────────────────────────────────────

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM resume_variants WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall()
        return [_public(r) for r in rows]

    def get(self, variant_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM resume_variants WHERE id=? AND user_id=?",
                          (variant_id, self.user_id)).fetchone()
        return _public(r) if r else None

    def default(self) -> dict | None:
        return next((v for v in self.list() if v["is_default"]), None)

    def text(self, variant_id: int) -> str:
        """The extracted text of a variant ("" if none or unreadable)."""
        with self._conn() as c:
            r = c.execute("SELECT text_enc FROM resume_variants WHERE id=? AND user_id=?",
                          (variant_id, self.user_id)).fetchone()
        return crypto.decrypt(r["text_enc"]) if r else ""

    def read(self, variant_id: int) -> bytes:
        """The decrypted PDF. ResumeError if there is no such variant, the file
        is missing, was never encrypted, or will not decrypt."""
        with self._conn() as c:
            r = c.execute("SELECT stored_name FROM resume_variants WHERE id=? AND user_id=?",
                          (variant_id, self.user_id)).fetchone()
        if r is None:
            raise ResumeError("no such resume")
        try:
            stored = (self.dir / r["stored_name"]).read_bytes()
        except OSError:
            raise ResumeError("the resume file is missing")
        if stored.startswith(PDF_SIGNATURE):
            raise ResumeError("the stored resume is not encrypted; refusing to use it")
        data = crypto.decrypt_bytes(stored)
        if not data.startswith(PDF_SIGNATURE):
            raise ResumeError("the resume could not be decrypted (is the encryption key the one it was stored with?)")
        return data

    def source(self, variant_id: int) -> "ResumeSource | None":
        v = self.get(variant_id)
        return ResumeSource(self, variant_id, v["filename"]) if v else None


# ── plaintext, only while it is needed ────────────────────────────

TEMP_PREFIX    = "apex-resume-"
STALE_SECONDS  = 6 * 3600


def sweep_stale_temp(max_age: float = STALE_SECONDS) -> int:
    """Delete decrypted temp resumes an earlier process left behind (it was
    killed mid-pass). Returns how many it removed. Never raises."""
    import time
    removed = 0
    try:
        for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
            try:
                if d.is_dir() and time.time() - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except Exception:
        logger.debug("[Resumes] temp sweep failed", exc_info=True)
    return removed


class ResumeSource:
    """A stored resume. It holds no plaintext: materialize() decrypts the PDF
    into a private temp file for the length of the `with` block, and deletes it
    afterwards (also when the block raises).

    The block must cover the whole pass that fills AND SUBMITS the form, not
    only the set_input_files call: the browser reads an uploaded file from disk
    when the form is sent, so deleting it straight after the upload would send
    an empty file. The Orchestrator wraps each filler pass in it."""

    def __init__(self, store: ResumeStore, variant_id: int, filename: str = "resume.pdf"):
        self.store, self.variant_id, self.filename = store, variant_id, safe_filename(filename)

    def exists(self) -> bool:
        try:
            self.store.read(self.variant_id)
            return True
        except ResumeError:
            return False

    @contextmanager
    def materialize(self):
        data = self.store.read(self.variant_id)
        sweep_stale_temp()
        tmpdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        try:
            os.chmod(tmpdir, 0o700)
            path = tmpdir / self.filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            yield path
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
