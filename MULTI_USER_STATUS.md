# Multi-user status

**Short version: this build is safe to deploy as a private, single-owner
instance behind a login. It is NOT yet safe to open sign-ups to other
people.** `ALLOW_SIGNUP` defaults to `false` for exactly that reason —
leave it that way until every item under "Blocking" is done.

## What's done and tested

| Area | State |
|---|---|
| Accounts | scrypt-hashed passwords, email normalisation, deactivation |
| Sessions | signed + expiring cookie, `HttpOnly`, `SameSite=Lax`, `Secure` (configurable) |
| Global auth gate | every HTTP route requires a session unless allow-listed (`/login`, `/api/auth/*`, `/static/*`); deactivated accounts lose access immediately |
| WebSocket | own session **and ownership** check (middleware can't see WebSockets) |
| Login brute-force | 5 failures / 15 min per (IP, email), 20 / 15 min per IP; returns `429` + `Retry-After` |
| Sign-up policy | first account always allowed; then closed unless `ALLOW_SIGNUP=true` |
| Jobs data | fully per-user (`JobsDB(user_id=…)`): reads, writes, stats, analytics, pipeline, bulk delete, ownership checks on notes/stages/briefs; pre-existing rows are adopted by account #1 |
| Secrets at rest | LLM API keys and saved job-site sessions encrypted (Fernet); key from `APP_ENCRYPTION_KEY` or an auto-generated `data/.master.key` |
| Placeholder secrets | every value shipped in this repo (`change_me`, `change_this_to_random_string`, empty, < 32 chars) is refused as a signing key |
| Job-site passwords | none stored anywhere, ever |

Run the tests: `python3 -m unittest discover -s tests -v`

**Verification caveat.** The database, hashing, encryption, throttle and
token logic ran and passed in the authoring sandbox — and the isolation
tests were mutation-checked (deliberately breaking the scoping makes them
fail). The **HTTP layer** (auth gate, route dependencies, WebSocket
check — `tests/test_http_auth.py`) could not be executed there because
FastAPI wasn't installable, so those 16 tests skip themselves. Run them
after `pip install -r requirements.txt`; if any fail, believe the test
first.

## Blocking — do before setting `ALLOW_SIGNUP=true`

Each of these currently serves **account #1's** data to any logged-in
user.

1. **Profile / resume** — `ProfileDB` is one global profile (name, email,
   phone, resume text). Needs `user_id`, like `JobsDB`.
   Files: `db/profile_db.py`, `server/routes/onboarding.py`.
2. **Apply flow** — `server/routes/apply.py`, `applier/session.py`,
   `applier/universal.py` build `JobsDB()` **unscoped**, keep session
   files in one global folder, and write screenshots (filled-in forms =
   personal data) to one shared, guessable-by-job-id directory.
3. **Tracking + ghostwriter routes** — `server/routes/tracking.py`,
   `server/routes/ghostwriter.py`, `tracking/pipeline.py` still call
   `JobsDB()` unscoped.
4. **Settings** — LLM keys are global. Decide: operator-level (you pay for
   everyone's LLM calls; settings become owner-only) or per-user
   bring-your-own-key. Either way non-owners must not read/write them.
5. **The browser** — the largest item, see below.

Until 1–4 are done, the owner-only guard (`require_owner` in
`core/auth.py`) blocks non-owners from running/cancelling the pipeline.

## The browser problem (why this isn't "just add a user_id")

`core/browser.py` launches **one specific Opera install with its real
profile** on the machine running the server. That is how no job-site
password is ever needed — and it means:

- every user would share one LinkedIn/Indeed/Naukri identity;
- it cannot run on a headless cloud VM (no Opera, no screen);
- the "log in / solve the CAPTCHA in the window that opens" step has no
  window to open on a server.

Two ways forward — a product decision, not a coding detail:

- **Remote browser streaming:** browser runs on your server; its screen is
  streamed into the user's tab so they can log in / solve CAPTCHAs
  remotely. Everything stays centralised, but *your server holds other
  people's live job-site sessions* — the highest-liability option.
- **Local companion app (recommended):** dashboard, scoring and profile
  stay on your server; browsing/applying runs in a small agent on each
  user's own computer. CAPTCHAs work as they do today, and you never hold
  their job-site sessions.

## Threat model, stated plainly

- Encryption at rest stops someone reading a copied database, backup or
  cookie file. It does **not** stop someone who controls the running
  server, because the process must be able to decrypt. For that you need
  the key held outside the app (a KMS/secrets manager) — the code is
  structured so only `core/crypto._load_or_create_key()` changes.
- The login throttle is in-memory and per-process: fine for one uvicorn
  process, not for several workers.
- Behind a reverse proxy, start uvicorn with `--proxy-headers` and
  `--forwarded-allow-ips=<proxy ip>` or every visitor shares one throttle
  bucket and one attacker can lock everyone out.
- Automated applying violates LinkedIn's and Indeed's terms of service.
  Running it for *other people* on your infrastructure makes their
  account suspensions partly your responsibility.
