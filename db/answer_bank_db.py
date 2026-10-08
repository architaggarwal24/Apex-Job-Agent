# db/answer_bank_db.py
"""
The answer bank: questions the agent has been asked on application forms and
the answers you gave, so it can answer the same question next time.

One row per (user, scope, company, normalised question):

    question_raw / question_norm   the wording as seen, and its normal form
    answer                         what to put in the field
    scope    global | company      company rows only apply to that company
    company                        the company name for company rows, else ""
    policy   always | ask | once   how the form filler uses it (applier/answers.py)
    source   user | ai | profile  who wrote it; profile entries are NOT rows -
                                   they are derived from the profile and read-only
    uses, last_used_at             how often / when it was applied

Matching (resolve): the question is normalised (lowercase, accents/punctuation
and stop words stripped), then EXACT matches are tried and only if there is
none, FUZZY ones (difflib ratio >= 0.88, and no differing word may be a
different word altogether: compatible()). Within either stage a company-scoped
entry beats a global one, and at the same scope what you wrote yourself beats
the AI's draft, which beats the profile.

Every query is scoped by user_id. There is deliberately NO unscoped view (the
way JobsDB(user_id=None) is): a bank read without a user would hand one
person's answers - salary, notice period, free-text essays - to another.

The profile is ONE global profile for the machine (db/profile_db.py), which
belongs to the first account, so profile-derived entries are only produced
for that user (PROFILE_OWNER_ID); everyone else's bank is rows only.
"""

import difflib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

SCOPES   = ("global", "company")
POLICIES = ("always", "ask", "once")
SOURCES  = ("user", "profile", "ai")
WRITABLE_SOURCES = ("user", "ai")          # "profile" is derived, never stored

FUZZY_THRESHOLD = 0.88
TOKEN_SIMILARITY = 0.75      # a word that differs must have a close counterpart (see compatible())
MAX_QUESTION_CHARS = 1000
MAX_ANSWER_CHARS   = 5000

# The single machine-wide profile belongs to the first account
# (core.auth.OWNER_USER_ID; kept in step by a test, since core.auth can't be
# imported from the db layer).
PROFILE_OWNER_ID = 1

PROFILE_PREFIX = "profile:"

class DuplicateAnswer(ValueError):
    """The change would give one question two answers in the same place."""


# ── normalisation ─────────────────────────────────────────────────

# Words that vary in how a form phrases a question without changing what it
# asks. Deliberately NOT stripped: negations, and when/where/why/who, which
# change the question.
STOP_WORDS = frozenset("""
a an the is are was were be been being am do does did doing have has had having
you your yours yourself we our ours us i my me mine he she it its they their them
of to for in on at by with from into as and or if so than then there
that this these those what which how many much
please kindly can could would will shall should may might any also just very
optional required mandatory enter select choose provide specify mention
whats youre dont didnt cant wont isnt arent ive im thats
""".split())

_LEGAL_SUFFIXES = {"pvt", "ltd", "limited", "private", "inc", "incorporated", "llc",
                   "llp", "corp", "corporation", "co", "company", "plc", "gmbh"}


def _words(text: str) -> list[str]:
    """Split on anything that is not a letter, digit or combining mark. (A
    regex \\w would cut Devanagari and other scripts apart at their vowel signs.)"""
    return "".join(ch if unicodedata.category(ch)[0] in "LNM" else " " for ch in text).split()


def normalize_question(text) -> str:
    """'What is your Notice Period? (in days) *' -> 'notice period days'.
    Unicode-aware (non-Latin labels keep their letters); apostrophes are
    dropped so "what's" is one token; stop words go. May be '' if nothing
    meaningful is left."""
    t = unicodedata.normalize("NFKC", str(text or "")).casefold()
    t = re.sub(r"['’`]", "", t)
    # Keep what punctuation stripping would otherwise destroy: c++, c#, .net,
    # node.js must stay different from each other and from plain "c". (A dot
    # between digits - "3.5 years" - is left alone.)
    t = re.sub(r"(?<=[^\W\d_])\+\+", "pp", t)
    t = re.sub(r"(?<=[^\W\d_])#", "sharp", t)
    t = re.sub(r"(?<![^\W_])\.net\b", "dotnet", t)
    t = re.sub(r"(?<=[^\W\d_])\.(?=[^\W\d_])", "", t)
    return " ".join(w for w in _words(t) if w not in STOP_WORDS)


def company_tokens(name) -> list[str]:
    """'Acme Corp.' -> ['acme']; 'Tata Consultancy Services Pvt. Ltd.' -> [tata, consultancy, services]."""
    toks = _words(unicodedata.normalize("NFKC", str(name or "")).casefold().replace("&", " and "))
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return toks


def normalize_company(name) -> str:
    return " ".join(company_tokens(name))


def similarity(a: str, b: str) -> float:
    """difflib ratio of two NORMALISED questions, 0..1."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def compatible(a: str, b: str) -> bool:
    """The guard on a FUZZY match. Whole-string similarity is blind to which
    word changed: "years experience cpp" vs "...csharp" scores 0.88, "5 years"
    vs "6 years" 0.95, Python vs PyTorch about the same. So every word that
    one side has and the other lacks must have a close counterpart (>= 0.75)
    on the other side: a typo or a plural passes ("experiance", "periods"),
    a different technology, number or place does not."""
    ta, tb = set(a.split()), set(b.split())
    for mine, theirs in ((ta - tb, tb), (tb - ta, ta)):
        for w in mine:
            if max((similarity(w, o) for o in theirs), default=0.0) < TOKEN_SIMILARITY:
                return False
    return True


def names_company(question: str, company: str) -> bool:
    """True if the question mentions the company: its name's words appear, in
    order and side by side, in the question ("Why Acme?" names "Acme Corp")."""
    want = company_tokens(company)
    if not want:
        return False
    have = company_tokens_raw(question)
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


def company_tokens_raw(text) -> list[str]:
    """Tokens of free text with NO suffix stripping (a question isn't a company name)."""
    return _words(unicodedata.normalize("NFKC", str(text or "")).casefold().replace("&", " and "))


# ── profile-derived (virtual, read-only) entries ──────────────────

# profile key -> the ways a form asks for it. The first phrasing is the one
# shown as the entry's question.
PROFILE_QUESTIONS: dict[str, tuple[str, ...]] = {
    "notice_period_label":  ("Notice period", "How soon can you join", "When can you join",
                             "Availability to join", "Joining time"),
    "notice_period_days":   ("Notice period in days", "Notice period (days)", "Serving notice period days"),
    "current_ctc_lpa":      ("Current CTC", "Current CTC (LPA)", "Current salary", "Present CTC",
                             "Current annual salary", "Current annual CTC"),
    "expected_ctc_lpa":     ("Expected CTC", "Expected CTC (LPA)", "Expected salary",
                             "Salary expectation", "Desired CTC", "Expected annual salary"),
    "current_ctc_monthly":  ("Current monthly salary", "Current CTC monthly", "Current monthly CTC"),
    "expected_ctc_monthly": ("Expected monthly salary", "Expected CTC monthly", "Expected monthly CTC"),
    "willing_to_relocate":  ("Willing to relocate", "Are you willing to relocate",
                             "Open to relocation", "Ready to relocate"),
    "work_mode_preference": ("Work mode preference", "Preferred work mode",
                             "Work arrangement preference", "Remote, hybrid or onsite"),
    "experience_years":     ("Total experience", "Total years of experience", "Years of experience",
                             "How many years of experience do you have", "Overall experience"),
    "authorized_to_work":   ("Authorized to work in India", "Are you legally authorized to work",
                             "Work authorization"),
    "require_sponsorship":  ("Require visa sponsorship", "Will you require sponsorship", "Need sponsorship"),
    "current_role":         ("Current job title", "Current designation", "Current role"),
    "location_city":        ("Current location", "Current city", "City", "Where are you located"),
    "linkedin_url":         ("LinkedIn profile", "LinkedIn URL"),
    "github_url":           ("GitHub profile", "GitHub URL"),
    "portfolio_url":        ("Portfolio", "Portfolio URL", "Website"),
    "highest_degree":       ("Highest qualification", "Highest degree", "Highest education"),
    "graduation_year":      ("Graduation year", "Year of passing"),
}


def profile_entries(profile: dict | None) -> list[dict]:
    """Read-only entries for the profile facts that have a value. `profile`
    is the flat {key: value} profile dict. id is 'profile:<key>'; there is one
    entry per key whatever its phrasings, and `phrasings` (internal) lists the
    normalised wordings it answers to."""
    out = []
    for key, phrasings in PROFILE_QUESTIONS.items():
        value = str((profile or {}).get(key, "") or "").strip()
        if not value:
            continue
        out.append({
            "id": PROFILE_PREFIX + key, "question_raw": phrasings[0],
            "question_norm": normalize_question(phrasings[0]), "answer": value,
            "scope": "global", "company": "", "policy": "always", "source": "profile",
            "uses": 0, "last_used_at": None, "created_at": None, "updated_at": None,
            "read_only": True,
            "phrasings": tuple(p for p in dict.fromkeys(normalize_question(p) for p in phrasings) if p),
        })
    return out


# ── the store ─────────────────────────────────────────────────────

_PUBLIC = ("id", "question_raw", "question_norm", "answer", "scope", "company", "policy",
           "source", "uses", "last_used_at", "created_at", "updated_at")


def _like(text: str) -> str:
    t = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


class AnswerBank:
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("AnswerBank needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH          # same SQLite file as the jobs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS answer_bank (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       INTEGER NOT NULL,
                    question_raw  TEXT NOT NULL,
                    question_norm TEXT NOT NULL,
                    answer        TEXT NOT NULL,
                    scope         TEXT NOT NULL DEFAULT 'global'
                                  CHECK (scope IN ('global','company')),
                    company       TEXT NOT NULL DEFAULT '',
                    policy        TEXT NOT NULL DEFAULT 'always'
                                  CHECK (policy IN ('always','ask','once')),
                    source        TEXT NOT NULL DEFAULT 'user'
                                  CHECK (source IN ('user','profile','ai')),
                    uses          INTEGER NOT NULL DEFAULT 0,
                    last_used_at  TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_answer_bank_user "
                      "ON answer_bank(user_id, question_norm)")

    # ── validation ────────────────────────────────────────────────

    @staticmethod
    def _clean(question, answer, scope, company, policy, source) -> dict:
        question = str(question or "").strip()
        answer = str(answer if answer is not None else "").strip()
        if not question:
            raise ValueError("question is required")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(f"question is too long ({MAX_QUESTION_CHARS} characters at most)")
        qn = normalize_question(question)
        if not qn:
            raise ValueError("question has no meaningful words")
        if not answer:
            raise ValueError("answer is required")
        if len(answer) > MAX_ANSWER_CHARS:
            raise ValueError(f"answer is too long ({MAX_ANSWER_CHARS} characters at most)")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {list(SCOPES)}")
        if policy not in POLICIES:
            raise ValueError(f"policy must be one of {list(POLICIES)}")
        if source == "profile":
            raise ValueError("profile answers are read-only; change them on the Profile page")
        if source not in WRITABLE_SOURCES:
            raise ValueError(f"source must be one of {list(WRITABLE_SOURCES)}")
        company = str(company or "").strip()
        if scope == "company":
            if not normalize_company(company):
                raise ValueError("a company-scoped answer needs a company")
        else:
            company = ""
        return {"question_raw": question, "question_norm": qn, "answer": answer, "scope": scope,
                "company": company, "policy": policy, "source": source}

    @staticmethod
    def _public(row) -> dict:
        return {k: row[k] for k in _PUBLIC}

    def _find(self, c, scope: str, company: str, qn: str, exclude_id: int | None = None):
        """The row (if any) already holding this question at this scope for this user."""
        cn = normalize_company(company) if scope == "company" else ""
        for r in c.execute("SELECT * FROM answer_bank WHERE user_id=? AND scope=? AND question_norm=?",
                           (self.user_id, scope, qn)).fetchall():
            if r["id"] != exclude_id and (scope == "global" or normalize_company(r["company"]) == cn):
                return r
        return None

    # ── CRUD ──────────────────────────────────────────────────────

    def add(self, question, answer, *, scope: str = "global", company: str = "",
            policy: str | None = None, source: str = "user") -> dict:
        """Save an answer. If this user already has one for the same question
        at the same scope (and company) it is UPDATED instead - the bank never
        holds two answers to one question in one place.

        policy: None (the default) means "always" for a new entry and "leave
        the policy alone" for an existing one, so correcting an answer you
        set to ask/once never quietly turns it into always. Pass a policy to
        set it."""
        v = self._clean(question, answer, scope, company, policy or "always", source)
        now = datetime.now().isoformat()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._find(c, v["scope"], v["company"], v["question_norm"])
            if row:
                keep = row["policy"] if policy is None else v["policy"]
                c.execute("""UPDATE answer_bank SET question_raw=?, answer=?, company=?, policy=?,
                                    source=?, updated_at=? WHERE id=? AND user_id=?""",
                          (v["question_raw"], v["answer"], v["company"], keep, v["source"],
                           now, row["id"], self.user_id))
                eid = row["id"]
            else:
                eid = c.execute("""INSERT INTO answer_bank (user_id, question_raw, question_norm,
                                       answer, scope, company, policy, source, created_at, updated_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (self.user_id, v["question_raw"], v["question_norm"], v["answer"],
                                 v["scope"], v["company"], v["policy"], v["source"], now, now)).lastrowid
            return self._public(c.execute("SELECT * FROM answer_bank WHERE id=? AND user_id=?",
                                          (eid, self.user_id)).fetchone())

    def get(self, entry_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM answer_bank WHERE id=? AND user_id=?",
                          (entry_id, self.user_id)).fetchone()
        return self._public(r) if r else None

    def entries(self, q: str = "", scope: str = "", source: str = "", company: str = "",
             limit: int = 500) -> list[dict]:
        """This user's entries, most recently used/changed first. Filters (ANDed):
        q (substring of question or answer), scope, source, company (normalised match)."""
        where, params = ["user_id=?"], [self.user_id]
        if scope:
            if scope not in SCOPES:
                raise ValueError(f"scope must be one of {list(SCOPES)}")
            where.append("scope=?"); params.append(scope)
        if source:
            if source not in SOURCES:
                raise ValueError(f"source must be one of {list(SOURCES)}")
            where.append("source=?"); params.append(source)
        if q.strip():
            where.append("(question_raw LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\')")
            params += [_like(q)] * 2
        limit = max(1, min(int(limit), 2000))
        with self._conn() as c:
            rows = c.execute("SELECT * FROM answer_bank WHERE " + " AND ".join(where)
                             + " ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC LIMIT ?",
                             tuple(params) + (limit,)).fetchall()
        out = [self._public(r) for r in rows]
        if company.strip():
            cn = normalize_company(company)
            out = [e for e in out if e["scope"] == "company" and normalize_company(e["company"]) == cn]
        return out

    def update(self, entry_id: int, **fields) -> dict | None:
        """Change an entry. Allowed: question, answer, scope, company, policy.
        None if it isn't this user's. ValueError for a bad value, or if the
        change would duplicate another entry (same question, scope, company).
        Editing the answer makes it yours (source 'user'): you vouch for it."""
        allowed = {"question", "answer", "scope", "company", "policy"}
        if set(fields) - allowed:
            raise ValueError(f"cannot change {sorted(set(fields) - allowed)}")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("SELECT * FROM answer_bank WHERE id=? AND user_id=?",
                            (entry_id, self.user_id)).fetchone()
            if cur is None:
                return None
            scope = fields.get("scope", cur["scope"])
            v = self._clean(fields.get("question", cur["question_raw"]),
                            fields.get("answer", cur["answer"]), scope,
                            fields.get("company", cur["company"] if scope == "company" else ""),
                            fields.get("policy", cur["policy"]),
                            "user" if "answer" in fields else cur["source"])
            if self._find(c, v["scope"], v["company"], v["question_norm"], exclude_id=entry_id):
                raise DuplicateAnswer("another answer already exists for that question in that scope")
            c.execute("""UPDATE answer_bank SET question_raw=?, question_norm=?, answer=?, scope=?,
                                company=?, policy=?, source=?, updated_at=? WHERE id=? AND user_id=?""",
                      (v["question_raw"], v["question_norm"], v["answer"], v["scope"], v["company"],
                       v["policy"], v["source"], datetime.now().isoformat(), entry_id, self.user_id))
            return self._public(c.execute("SELECT * FROM answer_bank WHERE id=? AND user_id=?",
                                          (entry_id, self.user_id)).fetchone())

    def delete(self, entry_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM answer_bank WHERE id=? AND user_id=?",
                             (entry_id, self.user_id)).rowcount == 1

    def touch(self, entry_id: int) -> None:
        """The entry was just used to fill a form."""
        with self._conn() as c:
            c.execute("UPDATE answer_bank SET uses=uses+1, last_used_at=? WHERE id=? AND user_id=?",
                      (datetime.now().isoformat(), entry_id, self.user_id))

    # ── matching ──────────────────────────────────────────────────

    def resolve(self, question: str, company: str = "", profile: dict | None = None) -> dict | None:
        """The best answer for `question` at `company`, or None.

        Candidates are this user's global entries, their entries for this
        company, and (if `profile` is given - the caller passes it only for
        the profile's owner) the read-only profile facts. EXACT normalised
        matches are tried first; only if there are none, FUZZY ones with
        ratio >= FUZZY_THRESHOLD that also pass compatible(). Ties break on: company scope over global,
        (fuzzy: higher ratio), your own answer over the AI's over the
        profile's, most recently changed. Does NOT count as a use.

        Returns {answer, confidence, entry_id, source, policy, scope, question}
        (entry_id is the row id, or 'profile:<key>'); confidence is 1.0 for an
        exact match, else the similarity ratio."""
        qn = normalize_question(question)
        if not qn:
            return None
        cn = normalize_company(company)
        cands: list[tuple[dict, str]] = []                    # (entry, one normalised wording)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM answer_bank WHERE user_id=?", (self.user_id,)).fetchall()
        for r in rows:
            if r["scope"] == "company" and (not cn or normalize_company(r["company"]) != cn):
                continue
            cands.append((self._public(r), r["question_norm"]))
        for e in profile_entries(profile):
            cands.extend((e, p) for p in e["phrasings"])

        scope_rank  = lambda e: 1 if e["scope"] == "company" else 0
        source_rank = lambda e: {"user": 2, "ai": 1, "profile": 0}[e["source"]]
        fresh       = lambda e: e["updated_at"] or ""

        exact = [(e, p) for e, p in cands if p == qn]
        if exact:
            e, _ = max(exact, key=lambda ep: (scope_rank(ep[0]), source_rank(ep[0]), fresh(ep[0])))
            return self._match(e, 1.0)
        scored = [(e, similarity(qn, p)) for e, p in cands if compatible(qn, p)]
        scored = [(e, r) for e, r in scored if r >= FUZZY_THRESHOLD]
        if not scored:
            return None
        e, r = max(scored, key=lambda er: (scope_rank(er[0]), er[1], source_rank(er[0]), fresh(er[0])))
        return self._match(e, round(r, 2))

    @staticmethod
    def _match(e: dict, confidence: float) -> dict:
        return {"answer": e["answer"], "confidence": confidence, "entry_id": e["id"],
                "source": e["source"], "policy": e["policy"], "scope": e["scope"],
                "question": e["question_raw"]}

    def entries_with_profile(self, profile: dict | None, **filters) -> list[dict]:
        """entries() plus the read-only profile entries (unless filtered out)."""
        rows = self.entries(**filters)
        if (profile and not filters.get("company") and filters.get("scope", "") in ("", "global")
                and filters.get("source", "") in ("", "profile")):
            q = (filters.get("q") or "").strip().casefold()
            extra = [{k: v for k, v in e.items() if k != "phrasings"} for e in profile_entries(profile)
                     if not q or q in e["question_raw"].casefold() or q in e["answer"].casefold()]
            if filters.get("source") == "profile":
                return extra
            rows = rows + extra
        return rows
