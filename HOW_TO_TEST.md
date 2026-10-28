<!-- HOW_TO_TEST.md -->
# How to test Apex Job Agent

A hands-on checklist that covers everything the app can do. Work through it top to bottom
on a fresh checkout; each numbered item is one thing to try, with what you should see.
Tick the box when it behaves as described.

Legend: **[LLM]** needs a working model provider · **[NET]** needs the internet or a real
job site · **[BROWSER]** opens a real Chromium/Opera window · **[OWNER]** only the first
account can do it · **[2ND]** needs a second account (see §1.4).

---

## 0. Before you start

### 0.1 Install

```bash
pip install -r requirements.txt      # now includes python-jobspy (login-free board search)
playwright install chromium
cp .env.example .env
cd frontend && npm install && npm run build && cd ..
```

- [ ] `npm run build` ends with the route list (`/`, `/applications`, `/insights`, `/profile`, `/settings`) and `frontend/out/` exists.

### 0.2 Automated suite (run first, run last)

```bash
python -m unittest discover -s tests -v
```

- [ ] Everything passes. Some tests are skipped on purpose: HTTP tests without `fastapi`/`httpx`, UI/theme tests without Node, SIGINT tests on Windows.
- [ ] Single module example: `python -m unittest tests.test_lifecycle -v`.
- [ ] Note that about 76 tests make live model calls; set a provider key in `.env` first or expect those to take longer/fail without one.

### 0.3 Start the server

| Command | Use it for |
|---|---|
| `python main.py` | normal use: serves the built UI at http://127.0.0.1:8000 |
| `python main.py --dev-ui` | editing the frontend: also runs `npm run dev`; open http://localhost:3000 |
| `python main.py --schedule` | UI + a daily search at `SCHEDULE_TIME` (default 09:00, `TIMEZONE` default Asia/Kolkata) |
| `python main.py --pipeline [--quick]` | one search run, then exit [NET][BROWSER] |
| `python main.py --parse` | parse `resume.pdf` into the profile [LLM] |

- [ ] Startup log shows `UI (built) → …`, the provider/model line, `Dry run: …` and `Press Ctrl+C to stop.`
- [ ] Without `frontend/out`: log says `UI not built yet…` and `/` redirects to :3000 only under `--dev-ui`.
- [ ] `APP_HOST` / `APP_PORT` in `.env` change the bind address.
- [ ] Opening from another machine over plain http: set `COOKIE_SECURE=false` or the login cookie is dropped (localhost works either way).

### 0.4 Ctrl+C / shutdown

- [ ] Press Ctrl+C once: `[Shutdown] Ctrl+C received — stopping background tasks (press Ctrl+C again to force-exit immediately)` … `[Shutdown] Exiting.`, exit code 0, prompt returns within ~8 s.
- [ ] Same with an Applications tab open (the live stream is connected): still exits on one Ctrl+C.
- [ ] Under `--dev-ui`: one Ctrl+C also ends the `[next]` process; port 3000 is free afterwards (`npm run dev` can be started again) and there is no `ECONNREFUSED` spam after the exit line.
- [ ] Ctrl+C twice quickly: `Force-exit requested.`, exit code 1.
- [ ] Windows: same behaviour in `cmd`/PowerShell; the Next process tree is gone from Task Manager.

### 0.5 Encryption key and secrets

Where things live: `data/users.db`, `data/settings.db`, `data/profile.db`, `data/jobs.db`,
`data/cookies/`, `data/sessions/`, `data/screenshots/`, `data/resumes/<user>/`,
`data/.master.key`, `data/.session_secret`, `logs/apex_YYYY-MM-DD.log`.

- [ ] First start with no key: log warns `Generated a new encryption key at data/.master.key … BACK IT UP`.
- [ ] `APP_SECRET_KEY` shorter than 32 characters, or a placeholder (`change_me`): warning, and a generated `data/.session_secret` is used instead.
- [ ] Changing `APP_SECRET_KEY` logs every browser out.
- [ ] **Wrong encryption key scenario** (this is the "Could not decrypt" case):
  1. Save an API key in Settings → AI model → AI providers (or an SMTP password / Telegram token under You & alerts → Notifications).
  2. Stop the server. Set `APP_ENCRYPTION_KEY` to a fresh key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  3. Start again. Expect **one** startup WARNING: `[Crypto] N saved secret(s) can't be decrypted and read as empty: <names>. APP_ENCRYPTION_KEY is set and differs from data/.master.key, which is then ignored…`
  4. Settings → AI model → AI providers shows that key as "Not set"; Job Site Sessions shows `unknown`; a resume variant download fails with "the resume could not be decrypted…". The server keeps running, nothing crashes, the key value is never printed.
  5. Remove the env var (or set it to the contents of `data/.master.key`) and restart: the secrets read again and no warning appears.

---

## 1. Accounts and sign-in

### 1.1 First account (owner)

- [ ] Open http://127.0.0.1:8000. The login page shows the Apex mark, the headline "Your job search, run for you. Nothing sent without you.", the animated flow `search → score → fill → you review → submit` that walks once and rests on "you review", and the note "Runs on your machine…".
- [ ] "No account? Create one" is shown (sign-ups are open while there are no accounts). Subtitle: "The first account becomes the owner."
- [ ] Empty fields → "Enter your email and password". Password under 8 characters → server message "Password must be at least 8 characters". Bad email → "Enter a valid email address".
- [ ] Valid sign-up lands on Today; the sidebar footer shows your initial, your email and a sign-out button.

### 1.2 Sign in / sign out

- [ ] Sign out (footer button) returns to the login page; the "Create one" link is now **gone** (sign-ups closed after the first account unless `ALLOW_SIGNUP=true`).
- [ ] Wrong password → "Invalid email or password". Enter in the password field submits.
- [ ] 5 wrong passwords in 15 minutes → "Too many failed attempts. Try again in N minute(s)." (restart clears it).
- [ ] Reload after signing in: still signed in (cookie `apex_session`, 14 days, HttpOnly).

### 1.3 Gate and old URLs

- [ ] Signed out, `curl -i http://127.0.0.1:8000/api/applications` → `401 {"detail":"Not logged in"}`.
- [ ] Signed out, visiting `/applications/` → redirected to `/` (login). `/login` itself also redirects to `/`.
- [ ] `/jobs`, `/jobs/?view=list`, `/apply`, `/tracking` → 307 to `/applications/` (query string kept). `/setup` → `/`.
- [ ] `/favicon.ico`, `/icon.png`, `/apple-icon.png` load signed out; the tab shows the Apex icon.
- [ ] `/screenshots/anything.png` and `/docs` are **not** public (401 / redirect when signed out).

### 1.4 Second account [2ND]

Set `ALLOW_SIGNUP=true` in `.env`, restart, sign up a second user in a private window.

- [ ] Login page shows "Create one" again. The second account is **not** the owner: on Today the queue/search buttons are disabled with "Only the owner account can do this."
- [ ] Owner-only API calls from this account → `403 "This action isn't available on shared accounts yet."`
- [ ] Its Applications board is empty: jobs are per user. Its answer bank and resume variants are its own.
- [ ] Settings → AI model, Job search and Applying show the yellow "You are not the owner account…" note; Runner tokens and Notifications sections say they are owner-only.

### 1.5 Account settings (Settings → You & alerts → Account)

- [ ] "Signed in as <email>".
- [ ] Change password: wrong current → "Current password is incorrect"; new < 8 → "New password must be at least 8 characters"; mismatch → "The new passwords don't match"; same as current → "New password must be different from the current one".
- [ ] Successful change: "Password changed. Every other device has been signed out; this one stays signed in." A second browser that was signed in now gets the login page.
- [ ] "Sign out of all devices" → confirm → "Signed out everywhere else."; this browser stays signed in, others are out.

---

### 1.6 Command palette (Ctrl+K / ⌘K)

- [ ] **Ctrl+K** anywhere (even while typing in a field) or the sidebar's **Jump to…** opens it; Ctrl+K again or Esc closes it. ↑ ↓ move, Enter runs, a click runs.
- [ ] Empty: **Actions** (Run a job search, Quick search, Score the Inbox, Switch theme, Sign out; *Cancel the running search* while one runs) and **Go to** (every page, every Applications tab, every Profile tab, every Settings page and fold).
- [ ] Typing ranks: every word must start a word ("api k" → Settings › Job search › API keys & Apify; "prof edu" → Profile › Education); a job's title or company finds it under **Jobs** and Enter opens it in Applications with its panel.
- [ ] **queue <words> [min fit]** — e.g. `queue data 90` → "Queue 3 Inbox jobs matching “data” with fit ≥ 90" (default 80); Enter queues exactly those (same rules as the Queue button: owner only, quality gate) → toast "Queued 3".
- [ ] A second account sees the owner-only actions marked "owner only"; choosing one says "Only the owner account can do this."

## 2. Profile

- [ ] Heading "Profile", subtitle "What the agent writes into application forms."; **Upload new resume** top right.
- [ ] A yellow strip "No resume on file yet…" shows until a resume is uploaded; afterwards "Resume on file…".
- [ ] Upload a non-PDF → "Only PDF files are accepted" (client side; the API returns 400 with the same text).
- [ ] Upload a PDF [LLM]: "Uploading & parsing…" then `✓ Parsed "<file>" — <name>, N skills found. Fields were overwritten — review every tab before saving.` `resume.pdf` appears in the project root.
- [ ] Tabs Personal / Professional / Education / Identity, one section on the page at a time; the URL follows (`/profile/?t=education`), a reload or pasted link opens that tab, an unknown `?t=` opens Personal. Each tab shows how many fields are still empty (fields marked optional don't count) and the number drops as you type. Fields come in a sensible order (Full Name first). Dropdowns for gender, relocation, work mode, authorisation, sponsorship, veteran and disability status.
- [ ] Edit a field in Professional and one in Education: both tabs get a yellow dot, the save bar reads "2 unsaved changes across 2 tabs" and "Save changes (2)"; switching tabs keeps the edits; one Save stores both.
- [ ] Phone width: the tabs scroll sideways in one row; the page itself never scrolls sideways.
- [ ] There is one **Full Name** field and no first/last name fields; after saving "Archit Aggarwal", `GET /api/profile/flat` shows `first_name` "Archit" and `last_name` "Aggarwal" (forms that ask for the halves still get them).
- [ ] **GitHub** and **LinkedIn** show the fixed part (`github.com/`, `linkedin.com/in/`) and take only the user name; pasting a full URL collapses to the user name, and the stored value is the canonical URL.
- [ ] Edit two fields: button reads "Save changes (2)"; after saving "✓ Saved 2 field(s)". Reload: values persist.
- [ ] Nothing changed → Save disabled.
- [ ] `python main.py --parse` does the same parse from the command line [LLM].

---

## 3. Settings

Five pages in a left menu: **Start here** · **AI model** (AI providers, AI job scoring) · **Job search** (Job boards, Job feeds, Company boards, API keys & Apify — checks under §4) · **Applying** (Who submits, Practice mode & pace, How many a day, Skip these, Answer bank, Resumes, Where it runs, Job-site logins, Auto-submit (advanced)) · **You & alerts** (Appearance, Account, Notifications). Each page is a column of folds; a closed fold shows its title and one line saying what is set now (e.g. "Practice mode ON — nothing is sent · waits 45–120 s", "2 companies blocked · under ₹12.5 LPA skipped"); the ones worth a look carry a green note ("Recommended: you approve", "Try practice mode first", "Advanced"). Every setting is in exactly one fold.

- [ ] `/settings` opens **Start here**: four numbered steps, each ticked when done — 1 Choose the AI model ("Using <provider> / <model>. Not tested yet — press Test on its card." → **Open AI providers**), 2 Add your resume and details (what is missing, → **Open Profile**), 3 Say what to search for (job titles and places typed right there → Save → the step ticks), 4 Decide who sends applications (in words, → **Change who submits** / **Change where it runs**). Below: "Everything else is optional" with a card per page.
- [ ] Clicking a fold opens it (one at a time) and the URL becomes `/settings/?s=<page>&open=<fold>` (e.g. `?s=applying&open=pace`); clicking it again closes it. Reloading or pasting the URL opens the same fold; an unknown `?s=` opens Start here. Old links still land: `?s=providers` → AI model › AI providers, `?s=agent` → Applying › Practice mode & pace, `?s=logins`, `?s=keys`, `?s=runner`, `?s=notifications` likewise.
- [ ] A fold's summary follows a save (e.g. Skip these after adding two companies).
- [ ] Phone width: the menu becomes one sideways-scrolling row; fold titles get the full width with the green note under them; the page itself never scrolls sideways.

### 3.1 You → Appearance (per browser, no save button)

- [ ] Theme: **Light** (default: soft lavender page, white cards, coral accent), **Dark** (deep aubergine), **Follow system** — one design ("Deck": Bricolage Grotesque headings and big numbers, Inter Tight text, JetBrains Mono data, rounded cards on a violet shadow) in two colourways. The sun/moon button at the bottom of the sidebar flips light ↔ dark from any page. After updating, run `npm install` in `frontend/` once (the fonts changed).
- [ ] Follow system: change the OS appearance while the app is open → it follows without a reload.
- [ ] Density (Compact / Comfortable / Spacious), Text size (Small … Extra large), Motion (Follow system / Reduce / Full).
- [ ] Reload: everything is kept (`localStorage["apex-prefs"]`, applied before first paint — no flash). A browser that still has an old theme saved (Graphite, Paper…) opens in Light.
- [ ] "Reset appearance options" restores the options but keeps the chosen theme.
- [ ] Phone width: the sidebar becomes a top row; Applications shows the list, and opening a job slides the pane over it.

### 3.2 AI → AI providers [OWNER]

- [ ] The active provider card is first; grey dot = "No API key yet", green = "Ready to use".
- [ ] Paste an API key; the save bar (only under AI providers) reads "1 unsaved change(s)"; going to another page keeps the edit and the AI model link gains a yellow dot.
- [ ] Save → "Saved 1 setting(s)". Reload: the key shows masked (`••••••••` + last 4) and is never returned in full (`GET /api/settings/raw/<key>` → 403).
- [ ] Saving placeholder text like `your_openai_key_here` → "That looks like placeholder text, not a real API key".
- [ ] Test [LLM][NET]: `✓ Connected to <model> in N ms — replied "…"`. A wrong key gives a readable ✗ error, a dead URL "Timed out waiting for the provider…". A successful test ticks the "llm" item of the Today checklist.
- [ ] Fetch models [NET]: list appears, filter box when > 12, clicking a model selects it and adds it to "Your models".
- [ ] Add a model by name; remove it with ×.
- [ ] "+ Add a provider": pick a preset (Groq, Together…), name + base URL required, Add → new card marked Custom; "Save name & URL" → "Details saved."; Remove → confirm → card gone.
- [ ] Temperature outside 0–1 → "Invalid value for 'llm_temperature'".
- [ ] A base URL pointing at a link-local/metadata address (e.g. `http://169.254.169.254`) is refused; loopback and LAN addresses are allowed on purpose (Ollama, LM Studio).
- [ ] [2ND] The second account can open this section but Save returns the 403 message.

### 3.3 AI model → AI job scoring [OWNER]

- [ ] "Score new jobs with the AI" (default off). Summary: "Off: jobs get the instant keyword match only" / "On: the AI scores each new job". Save → "Saved 1 setting".

### 3.4 Applying and You & alerts [OWNER unless noted]

Each fold has its own Save and shows its result inline.

**Who submits**
- [ ] Two cards: "The agent fills it, you approve" (recommended) and "I apply myself". Pick one → Save → the fold's summary changes.
- [ ] "Different for some kinds of site" (closed by default) holds the per-site-type overrides (defaults: `indeed_apply` and `linkedin_easy_apply` = I apply myself). Save, reload, still there.

**Practice mode & pace**
- [ ] Practice mode switch (`dry_run`): when on, Today shows a "Dry run" chip in the header and the summary says "Practice mode ON — nothing is sent".
- [ ] Wait at least / at most (seconds, defaults 45/120). Save → "Saved N settings"; values survive a restart.

**How many a day**
- [ ] Careful mode (recommended) on: hint says the day is capped at 10 and the queue only takes scored jobs at/above the threshold.
- [ ] Most applications per day (0 = no limit) and Minimum fit score (0–100). Live line "In force now: a of l today" (+ "(dry run: the cap is not enforced)" in dry run).
- [ ] Out-of-range threshold → 400 from the server shown inline.

**Where it runs** (with the runner tokens)
- [ ] Three cards: My computer (recommended) / My computer when it is on, else this server / This server (`executor` runner / auto / server). Save → summary follows.

**Auto-submit (advanced)**
- [ ] Kill switch is OFF by default ("OFF - everything waits for you"). Toggle → "Autonomy is ON".
- [ ] Add rule (defaults: every site type, score 70, 5/day, 20 reviewed attempts, 90 % success, every answer known) → "Rule added".
- [ ] Preview under the rule: "Queued now: X of Y would auto-submit" and per-job ✓/✕ with reasons such as "autonomy is switched off", "only N reviewed attempts, needs 20".
- [ ] Edit a field → Save rule; Disable / Enable; Delete → confirm "Delete rule #id?".
- [ ] Bad values (score 101, max per day 0) are refused with the server's message.

**Reliability ledger**
- [ ] Table per site type: Reached review, Reviewed, Unchanged, Edited, Rejected, Auto-approved, Success (last 20), Failures. Values change after you approve/edit/skip reviews in §5.

**Answer bank** (any account, own data)
- [ ] Add question + answer (scope every company / one company, policy always / ask / once) → "Saved". Same question again at the same scope updates it instead of duplicating.
- [ ] Search box filters; scope filter; Edit → Save; Delete → confirm.
- [ ] Entries "from your profile" are read-only (editing via API → 403 "Profile answers are read-only…").
- [ ] "used N×" grows after an answer is used by an apply (§5).

**Resume variants** (any account, own data)
- [ ] Upload a PDF with label and tags; "make it the default" → "default" badge. "Uploaded "<label>"".
- [ ] A non-PDF renamed `.pdf` → "the file is not a PDF…"; > 5 MB → "The file is larger than 5 MB"; 11th variant → "you already have 10 resumes; delete one first"; duplicate label refused.
- [ ] Make default / Delete (confirm). Files are stored encrypted under `data/resumes/<uid>/`.

**Runner tokens** [OWNER]
- [ ] Create token with a label: the `apxr_…` token is shown **once** with "Copy the token… it will not be shown again"; Copy → "Token copied"; "I have saved it" hides it.
- [ ] Table: Label, Status (offline until a runner connects, online afterwards, revoked), Created, Last seen (refreshes every 15 s).
- [ ] Revoke → confirm → "Revoked"; a runner using it stops with exit code 2.
- [ ] 11th active token → "you already have 10 active runner tokens; revoke one first".

**Notifications** [OWNER]
- [ ] Send e-mail toggle + SMTP host, port, security (STARTTLS / SSL/TLS / none), user, password, from, to. Send Telegram toggle + bot token, chat id.
- [ ] Secret fields show a "set / not set / will be cleared" badge and never show the value; Clear empties it on Save.
- [ ] Validation: bad bot token → "That doesn't look like a Telegram bot token (123456:ABC...)"; one quiet-hours time only → "Give both quiet-hours times, or neither"; port 70000 refused.
- [ ] Event toggles: needs you, ready for review (digest), follow-up due, interview within 24 h, autonomy submitted. Quiet hours start/end and "No quiet hours".
- [ ] Save → "Saved"; "Ready now: e-mail, telegram" updates.
- [ ] Test e-mail / Test Telegram [NET]: "Test e-mail message sent" or a readable error; with empty fields: "Fill in the e-mail settings (host, to, and from or user name) first".
- [ ] Real delivery: with a job in needs_you (§5) a message arrives within ~1 minute, at most one per kind per 10 minutes, none during quiet hours.

**Skip these**
- [ ] Never apply to these companies "Tata" (one per line or comma-separated). Minimum salary in LPA (0 = off). Save.
- [ ] Effect is checked in §5.2 preflight.

### 3.5 Applying → Job-site logins

- [ ] Rows LinkedIn / Indeed / Naukri with a status dot: grey not logged in, green logged in ("Session saved Nh ago"), yellow expired, red unknown.
- [ ] After a pipeline run where you logged in (§4) the row turns green.
- [ ] Clear session → confirm → grey again; `data/cookies/<site>.json` is removed.

---

## 4. Today

- [ ] Heading "Good morning / afternoon / evening" + long date; chip with provider/model; "Dry run" chip when on. **Run search** and **Quick run** sit top right; while a search runs Quick run becomes **Cancel**.
- [ ] **Set up Apex** checklist (resume, profile, search prefs, llm, login, search): "N of 6 done" with a thin progress line; click the header to fold it. "Go" links: the LLM step opens Settings → AI model → AI providers (`/settings/?s=providers`), the login step Settings → Applying → Job-site logins, resume/profile open Profile. It disappears once all six are done.
- [ ] Search prefs inline: Save is disabled until both titles and locations have a value → "Search preferences saved".
- [ ] **Counts** strip (counts animate up): To triage (Inbox), Needs you (yellow dot, pulses when > 0), Ready to review, Interviews in 48 h, Follow-ups due. Each tile is a link to the matching filter (`?needs_you=1`, `?ready_for_review=1&view=review`, `?stage=recruiter_screen,technical_round,hiring_manager&next_step_within=48`, `?followups=due`).
- [ ] **Waiting on you**: blocked jobs (with the reason as the badge) then ones ready to review, best score first, each a link into Applications; "No job is waiting on you…" when none.
- [ ] **Follow-ups due**: "No follow-ups due. Nice." when empty. With items: kind (Check in / Thank you / Ask for news), link to the job, due time, draft text; Copy → "Draft copied", Done → "Marked done", Snooze 1/3/7 days → "Snoozed for N day(s)". The draft is written by the model when missing [LLM].
- [ ] **Queue** card: state badge (stopped / paused / working / running), "N queued", Pause/Resume [OWNER] → "Queue paused" / "Queue resumed" (survives a restart), today's budget bar, "Next apply" countdown ticking every second.
- [ ] **Job search** [OWNER][NET]: Run search → "Starting…" then the toast "Search running in the background…". With the default engines (`search_engine_linkedin|indeed|naukri` = `jobspy`) **no browser window opens and no login is needed**: LinkedIn, Indeed and Naukri are searched through python-jobspy. The log shows `linkedin (jobspy): N listings, M with a company apply form`. **Locations name your country:** a saved "Delhi" is searched as "Delhi, India" (your profile country), "Remote" as remote jobs in India, and results in another country that are not remote (e.g. "Ann Arbor, MI, USA", "Delhi, MI, USA") are dropped (`server.log`: "N outside India and not remote dropped"). A location that names a country on purpose ("Berlin, Germany") keeps that country's results. Same for the Apify engine. New jobs appear in Applications as they are scored. Quick run uses 2 titles × 2 locations. Cancel → "Cancel requested…".
- [ ] **Settings → Job search → Job boards**: set a board's engine to `Browser — your saved login`: that board opens the old browser window and asks for the login once [BROWSER]; the others stay login-free. `Apify actor — needs a token` runs the Store actor named under API keys → "Apify" with your token (free plan, $5/month usage) [NET]; with no token the search log says `naukri: FAILED — no Apify token…` for that board only. Bad values are refused ("search engine must be one of: jobspy, apify, browser"; an actor must be `owner~name`); "Results per title × location" 1–200 and "Only postings newer than" 0–8760 h are validated the same way.
- [ ] **Settings → Job search → API keys & Apify**: Adzuna app id + key (developer.adzuna.com, free), JSearch key (RapidAPI, 200 free calls/month), Himalayas key (optional). Saved keys show masked (`••••1234`) and a "set" badge; `GET /api/settings/raw/jsearch_api_key` → 403. Until a key is there the source line says "needs a key" and the search log says `skipped (needs a key in Settings (…))` rather than failing.
- [ ] **Settings → Job search → Job feeds**: one switch per feed and per ATS vendor (21 in all), each with its last run ("never run" / time — N found / error); untick Remotive → Save → `GET /api/settings` has `"search_sources": {"remotive":false}`. **Company boards** (its own section): table of boards followed (company, ATS, slug, learnt/you, last run), Pause/Resume, Remove (confirm), and "Follow a board" by pasting a link (`https://boards.greenhouse.io/<slug>/…`) or picking the ATS + slug; a LinkedIn link is refused with "That link is not a company board on a supported ATS…". A second account sees every section read-only.
- [ ] **Login-free sources** (incl. Adzuna and JSearch once their keys are saved) run after the boards in the same search (log: "Searching Himalayas…", "Searching N Greenhouse board(s)…"): Himalayas, Jobicy, Remotive, Remote OK, We Work Remotely, HN "Who is hiring?", Arbeitnow, plus every company board on Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Teamtailor, Personio, Breezy, Pinpoint, BambooHR and Polymer that the agent follows. `GET /api/sources` lists each with `enabled` and its last run (found / error); `POST /api/settings {"search_sources": {"remotive": false}}` turns one off (unknown names → 400). Remotive/Jobicy/HN are throttled between calls — a second search within the window logs "skipped (asked N min ago…)".
- [ ] **Company boards are learnt automatically**: after a search whose results link to e.g. `boards.greenhouse.io/<slug>/…`, `GET /api/sources/companies` shows that slug with `origin: "harvest"`; the next search reads the board itself. Add one by hand: `POST /api/sources/companies {"url": "https://jobs.lever.co/<company>"}` or `{"vendor":"ashby","slug":"<org>"}` (owner only; 409 if already followed, 400 for an unknown ATS link); `PUT …/{id} {"enabled": false}` pauses it; `DELETE …/{id}` removes it; a second account gets 403 on writes and its own empty list.
- [ ] Listed-only sources are filtered by your search titles and locations (a "Backend Engineer" search keeps "Senior Backend Engineer (Python)" in Bengaluru or Remote, not a Berlin on-site role); the same opening seen on Himalayas and on the company's Greenhouse board is stored once.
- [ ] A LinkedIn/Indeed posting that applies on the company's site shows `→ greenhouse` (or lever / ashby / the host) on its card and an **Apply form ↗** button next to "Open posting" in the panel's Overview; `GET /api/applications/{id}` carries `apply_url` and `posted_at`. The agent applies on that form (no job-board account); the same form seen on two boards is kept once.
- [ ] A finished run ticks the checklist "search" item and the pipeline writes a brief for the top 10 jobs [LLM].
- [ ] Everything on the page refreshes without reload when applications change (live events) and every 30 s otherwise.

---

## 5. Applications

Seed data without the internet: `POST /api/applications/queue` etc. need real jobs, so either
run one search (§4) or insert rows the way the tests do (`tests/` have helpers that create
jobs through `db.jobs_db`). The checks below assume at least a handful of jobs in `found`.

### 5.1 Workspace: tabs, list, keys

- [ ] The page fills the window and never scrolls as a whole: the list and the job pane scroll on their own.
- [ ] Header: "N to triage · N need you · N to review · N applied", live dot ("live" green = SSE, "polling" yellow), and a ⋯ menu with **Purge dupes** (confirm "Remove duplicate jobs?") and **Clear all jobs**.
- [ ] Tabs with counts: **Inbox** (new jobs, the default) · Queued · In progress · Needs you · Review · Applied · Archive (skipped + failed). The counts follow the search and filters.
- [ ] **Inbox opens as a deck of cards** (Cards / List switch beside the filters; List is `?view=list` and survives a reload). One job at a time, best fit first: company · place · age, big title, "Applies on greenhouse — no login" (or the board), salary, the fit (solid number = AI, outlined = quick match) with *why*, and the start of the posting. Behind it the next card; left "Up next" (next six); right: the live search, what is **Queued** (and "applying now…"), what **Needs you**.
- [ ] Decide: **→ / Q** or drag the card right = queue (a green "Queue →" stamp grows as you drag); **← / X** or drag left = skip; **↓ / L** = later (to the back of the deck); **↑ / Enter** or *Details* = the job's panel (Esc closes); **O** opens the posting. The card flies off at once — nothing waits for the server.
- [ ] Every queue / skip (cards, list, bulk, panel) shows a dark toast "Queued “AI Engineer”" with **Undo (Z)** and a 6-second fuse. Undo puts the job back where it was (an unqueue, or skipped → Inbox) and back on top of the deck: "Back where it was: “AI Engineer”". Skipping a job that needed you can't be undone by a legal move, so no Undo is offered there.
- [ ] Something the server refuses moves back by itself with the reason: e.g. queue a job scored 25 → "Not queued: Score 25 is below the threshold of 30" and it stays in the Inbox.
- [ ] **Live while a search runs**: jobs are saved as each board / the feeds return them (not all at the end), each with its pre-filter and quick match, so the Inbox fills during the search. An open Applications page shows "↑ N new jobs found" beside the filters and the Inbox count ticks up; the card you are reading stays on top (a better job arriving does not swap it). Click the pill to bring the new ones up (list rows that arrived flash once). The AI scoring (when on) still runs once, after the search.
- [ ] While data loads, pages show grey shimmering placeholders in the shape of what's coming (rows, cards) instead of "Loading…".
- [ ] After a few decisions the header shows "N to go · M decided · about K min left at your pace". At the end: "Inbox zero." with a link to run a search. A second account sees its own (empty) deck; the Queue button is disabled for it.
- [ ] Toolbar: search (title/company/place, `?search=`), **Filters** (with the number active), your first saved views as one-click pills, **Company form** (`?form=1`).
- [ ] **Filters** opens a panel on the right; every change applies at once and the footer reads "Show N of M jobs". Sections: **Where** (location contains, Remote / Hybrid / On-site), **Money & freshness** (minimum salary in LPA — read from the posting: "₹10L–₹15L", "12–18 LPA", "₹50,000 a month" = 6 LPA; "Hide jobs that don't state a salary"; posted within 24 h / 3 / 7 / 30 days, else when found), **Fit** (score between, scored by AI / quick match / not scored, seniority from the title), **Company & source** (only / never these companies, comma-separated; which boards; applies on greenhouse / lever / … / the board itself; company form only). Esc or the footer button closes it.
- [ ] Every filter is in the URL (`?work=remote&sal=12&age=7&kind=ai&level=senior&co=…&xco=…&source=linkedin,indeed&ats=greenhouse,none`) and shows as a chip under the toolbar with ✕; **Clear all** keeps the search text and the tab.
- [ ] **Saved views**: name the current filters in the panel → "Saved the view “Remote AI ≥ 70”" (saved on the server, per account, so it is there on another device; same name replaces it; at most 30). Apply from the panel or the toolbar pill; delete with ✕. Another account never sees yours. The cards deck shows only the filtered jobs too.
- [ ] List: one row per job — fit (number + 5-segment meter), title, company · place, "→ greenhouse" when it applies on a company form, source, age; sorted by fit (click a column header to sort; again to flip). Over 150 rows → "Show N more".
- [ ] **Every job has a fit score, even with AI scoring off.** New jobs get an instant *quick match* from keywords (your search titles and current role, resume skills, seniority vs. years of experience, location) — drawn with an **outlined** meter, tooltip "Quick match from keywords"; the pane's "Why this score" starts "Quick match (keywords): title matches “AI Engineer”; 3 of your skills mentioned (…)". An AI score (solid meter) replaces it when AI scoring is on. A quick match never auto-skips, never passes the quality gate and never triggers autonomy: those still need an AI score (`GET /api/applications/{id}` keeps `ai_score` null, `fit_kind` "quick").
- [ ] **Score Inbox** (header, owner): scores every Inbox job at once → "Scored N Inbox jobs by keywords. For a closer look, turn on AI scoring in Settings → AI model." With AI scoring on it then scores the Inbox jobs without an AI score in the background on the progress bar ("Scoring with AI · 3 of 40", then "Scoring finished — 40 jobs scored by AI") and the list refreshes; during a search it says AI scoring waits. Disabled for other accounts (the server returns 403).
- [ ] List keys (not while typing): `j`/`k` next/previous **and open it**, `q` queue (owner), `x` skip, `z` undo, `o` open the posting, `space` select, `Esc` clear selection / close the pane. After `q` or `x` the next job opens at once. A failed job can be re-queued but not skipped (the lifecycle doesn't allow failed → skipped).
- [ ] Bulk: tick rows (or the header box for all) → bar "N selected · Queue · Skip · Clear". Queue reports what was refused ("Queued 5 — 2 not queued: Score 25 is below the threshold of 30").
- [ ] Link-ins from Today: `?needs_you=1` → Needs you tab; `?ready_for_review=1&view=review` → Review queue; `?stage=recruiter_screen,technical_round,hiring_manager&next_step_within=48` → Applied, narrowed; `?followups=due` → "Filtered: every stage". A mixed `?stage=found,offer` shows "Filtered: Found, Offer".
- [ ] Applied tab: List / **Board** toggle; the board has one column per post-submission stage, each scrolling on its own; drag a card between them (illegal drops are refused with a message).
- [ ] Pipeline analytics moved to the bottom of **Insights**.
- [ ] Open a second browser tab on Applications; queue a job in the first: the second updates within a second (SSE).

### 5.2 Side panel

Click a card: a panel slides in from the right with title, "company · location · source", tabs
Overview · Timeline · Notes · Next step · Prep · Actions · (Review) · Assistant. `?job=<id>` is in the URL.

**Overview**
- [ ] Stage chip, days in stage, stale badge, ⚠ reason; "Open posting ↗" (or "No link"), "Email contact".
- [ ] AI score + reason, AI brief, description.
- [ ] Move to a stage: pick a stage, optional note, Move → "Moved to <Label>". An illegal move (e.g. found → offer) is refused with "Illegal transition found -> offer. Allowed from found: […]".
- [ ] Allowed map to spot-check: found→queued/preparing/submitted/skipped; submitted→recruiter_screen/technical_round/hiring_manager/offer/rejected/withdrawn; rejected/withdrawn → nothing.
- [ ] Moving to submitted stamps "Applied" and creates check-in follow-ups (+7, +14 days); to recruiter/technical/hiring manager creates a Thank-you follow-up for tomorrow; to technical/hiring manager/offer starts prep generation [LLM][OWNER].

**Timeline**
- [ ] Newest first, "date · actor", "From → To", notes, screenshot links after an apply.

**Notes**
- [ ] Add note → appears with timestamp; empty note refused.

**Next step**
- [ ] Next step text, date, time (enabled once a date is set), contact name/email, last contact; Save → "Saved" (hint: a date also schedules a follow-up).
- [ ] Bad email → "contact_email is not an email address"; bad date → "next_step_at must be a date (2026-11-03) or a date-time (2026-11-03T14:30)".
- [ ] A next step within 48 h on a recruiter/technical/hiring-manager job raises the Today "Interviews in 48 h" count.
- [ ] Follow-ups for this job: Copy draft, Snooze…, Done, Dismiss, Regenerate draft [LLM][OWNER] → "Draft rewritten". Done twice → "This follow-up is already done".

**Prep** [LLM][OWNER]
- [ ] Technical round / Hiring manager / Offer sections with status badge (not generated → pending → generating → ready), Generate/Regenerate, Copy. Second account → "Generating prep uses the owner's LLM…".
- [ ] Your offers (annual): add Company + components → "Offer added"; the table computes Recurring, First year and "vs best"; Delete removes it.

**Assistant** [LLM]
- [ ] Chat: greeting names the job and company; Send a question; errors appear inline and the text returns to the box.
- [ ] Cover letter: tone Professional/Enthusiastic/Concise → Generate → Copy.
- [ ] Interview: Generate → Technical / Behavioural / Company Q&A.

### 5.3 Drag and drop (board)

- [ ] Drag a Submitted card to Recruiter screen → "<title>: moved to Recruiter screen"; the timeline gets an event.
- [ ] Drag a Found card anywhere, or a Submitted card onto Found → "Cards can only be dragged between post-submission stages (… is not one). Use the panel's actions instead."
- [ ] Drag Offer → Submitted (illegal order) → the server's "Illegal transition…" message, card stays put.

### 5.4 Queue and apply flow (Actions tab)

Turn on **Dry Run** first (§3.3) so nothing is sent.

- [ ] Found card → Actions: "Ready to apply" text, Start application, Add to queue, Assist pack.
- [ ] Add to queue → "Queued", card moves to Queued; with Quality first on, an unscored or below-threshold job is refused ("Not scored yet…" / "Score N is below the threshold of T"). Remove from queue → "Back to found".
- [ ] Queue worker [OWNER][BROWSER][NET]: with the queue resumed, the highest-scored queued job moves to Preparing on its own, then to Needs you or Ready for review; Today shows "working on one now" and a random min..max delay before the next.
- [ ] **Preflight** (no browser opens, card goes to Skipped with the reason in the timeline): blacklisted company → "Company is on your blacklist (…)"; salary below the floor → "Salary … below your floor of F LPA"; same title+company already sent → "Already applied: …"; closed posting [NET] → "Job no longer open: …".
- [ ] **Assist mode** (LinkedIn Easy Apply / Indeed by default, or apply mode = assist): no browser; the job goes straight to Ready for review with the assist pack: Open the site ↗, Copy all, per-row Copy, "(upload this file)" rows, Mark applied (note) → Submitted, or Mark unavailable (reason required) → Skipped.
- [ ] **Auto mode** [BROWSER][LLM]: Start application → "Started — the agent is opening the application"; the step strip (Analyze · Fill · Your input · Re-fill · Review · Submit · Done) and the log fill live ("→ analyzing", "→ filling"…).
- [ ] **Needs you — question**: the form's unknown fields are listed; dropdown/inputs, "Agent tried: …" hints, "Pre-filled from your saved answer" when the answer bank knows one; "Remember this answer" ticked by default; Submit & continue → "Sent N answer(s)…", the agent re-fills. Remembered answers appear in Settings → Applying → Answer bank.
- [ ] **Needs you — captcha / login required**: "Open the site ↗", instruction text, Retry. **job_closed**: hint to mark unavailable. Other reasons: Retry + "Apply by hand (assist pack)".
- [ ] A failed job (timeout) shows "This application failed" with Retry; the queue retries it once after 60 s.
- [ ] Skip this job → confirm → "Skipped".
- [ ] Starting the same job twice at once → "An operation is already running for this job". Starting a submitted job → `already_done`.
- [ ] The `/ws/<job_id>` stream pings every 25 s; a job that isn't yours closes the socket (code 1008).

### 5.5 Review (Review tab and Review queue view)

- [ ] Ready for review opens on the Review tab: field table (flagged first, "⚑ reason", required *), Source badge (you / answer bank / profile / AI), Confidence %, screenshots carousel (‹ Page N ›, click opens full size).
- [ ] Click a value to edit; "Remember this answer" → Save → "Saved and remembered (global|company)" or "Saved for this application only". Enter saves, Esc cancels.
- [ ] Resume & cover letter: resume dropdown (variants from §3.4) → "Resume changed for this application"; Rewrite/Write one → "Cover letter rewritten" [LLM][OWNER]; Copy.
- [ ] Approve → "Approved — dry run: the form is filled but not submitted" (dry run) or "Approved — submitting" (real). After a real submit the card moves to Submitted and "Applied" is stamped. In dry run it stays in Ready for review and the log ends with "Dry run complete — nothing was submitted".
- [ ] Corrections in your own words → Correct & submit → "Submitting with your corrections".
- [ ] Skip → confirm "Skip <title> at <company>?" → "Skipped".
- [ ] Shortcuts in review context: `a` approve, `e` edit first flagged, `s` skip, `n` next, `?` help box ("Review keys …").
- [ ] "next" opens the next highest-scored review; when none is left: "The review queue is empty." and the panel closes.
- [ ] Review queue view: list with score, "clean" / "N flagged" / "no packet" badges; "Approve all unflagged (N)" → confirm → progress "Approving i of N: …" → "Approved N of N"; if nothing is clean: "Every application in the queue has a flagged field…".
- [ ] Limits (dry run off, caps set low): the 11th/limit+1 approve of the day → "Daily apply limit reached (n/limit)…"; approving two within the min delay → "Please wait Ns before submitting another application…".
- [ ] Each review decision (unchanged / edited / rejected) changes the Reliability ledger in Settings → Applying → Auto-submit (advanced).

### 5.6 Autonomy end to end [OWNER]

- [ ] With autonomy OFF, a rule's preview says "autonomy is switched off" for every queued job.
- [ ] Turn it ON, lower the rule to min reviewed attempts 1 and success rate 0.01 (test only), queue a scored job on a site type with at least one approved review: when it reaches review it is approved automatically; the timeline says so and Notifications "Autonomy submitted one" fires.
- [ ] Any classified failure on that site type disables the rule ("disabled" badge with the reason).

---

## 6. Insights

- [ ] Heading "Insights", "N submitted application(s) so far. Rates appear once a group has M."
- [ ] A strip of the overall numbers: Submitted, Reply / Interview / Offer rate, First response (median); "—" while there is not enough data.
- [ ] **Outcomes by …** table with a Source / Title / Score band / Resume variant switch: one row per group with Sent, Reply / Interview / Offer bars (hover shows "n of N submitted") and First response. Below the minimum a row shows "⚠ Not enough data — n of the M applications needed…" instead of any number or bar.
- [ ] Auto-skip calibration sits beside the table (below it on a narrow window); Pipeline analytics (funnel, by source, score distribution) underneath.
- [ ] Auto-skip calibration: reason text, Current vs Recommended threshold, "Apply this threshold" [OWNER] → confirm → "Auto-skip threshold set to N"; disabled when already in use (tooltip "Already in use").
- [ ] Reply rate by score band bars with the overall-rate marker; underperforming bands are highlighted.
- [ ] Move a few jobs to Submitted → Recruiter screen → Offer and watch the numbers change.

---

## 7. Runner (apply from your own computer) [BROWSER]

1. Settings → Applying → Where it runs → Runner tokens → Create token; copy it.
2. Executor = runner (or auto).
3. On the other machine (a checkout of this repo):
   `python runner/runner.py --server http://<server>:8000 --token apxr_... [--profile-dir PATH]`

- [ ] The runner prints that it is connected; the token row turns "online" within 15 s; `auto` executor now uses it.
- [ ] Start an application: the browser opens on the runner machine, events and screenshots flow back into the panel log/timeline, the result lands the job in Needs you / Ready for review exactly as a server run would.
- [ ] Captcha / login on the runner: the window comes to the front and waits (up to 10 min, 3 times).
- [ ] Wrong or revoked token → exit code 2 with `401 Invalid or revoked runner token`. No browser available → exit code 3.
- [ ] Stop the runner mid-task (Ctrl+C): the task is re-queued and picked up when the runner returns (lease 120 s, 3 claims); after 30 min unclaimed the job goes to Failed (timeout). Stopping it during the final submit sends the job to Needs you with "Your runner went quiet while submitting…".
- [ ] Plain http to a non-localhost server prints the "token travels unencrypted" warning.

---

## 8. API spot-checks (curl)

Sign in once and reuse the cookie:

```bash
curl -c c.txt -H 'Content-Type: application/json' -d '{"email":"you@example.com","password":"…"}' http://127.0.0.1:8000/api/auth/login
curl -b c.txt http://127.0.0.1:8000/api/auth/me          # {"authenticated":true,"id":1,"email":…,"owner":true}
curl -b c.txt 'http://127.0.0.1:8000/api/applications?stage=found&limit=5'
curl -b c.txt 'http://127.0.0.1:8000/api/applications?stage=nope'   # 400 Unknown stage: 'nope'
curl -b c.txt -N http://127.0.0.1:8000/api/events/stream  # ": keep-alive" every 15 s, application_event lines on changes
curl -b c.txt http://127.0.0.1:8000/api/apply/limits
curl -b c.txt http://127.0.0.1:8000/api/queue/status
curl -b c.txt http://127.0.0.1:8000/api/automation/ledger
curl -b c.txt -X POST -H 'Content-Type: application/json' -d '{"question":"Notice period","company":""}' http://127.0.0.1:8000/api/answer-bank/resolve
curl -b c.txt -X POST -H 'Content-Type: application/json' -d '{"queue_paused":true}' http://127.0.0.1:8000/api/settings   # 400 managed through its own endpoint
curl -b c.txt http://127.0.0.1:8000/api/settings/raw/openai_api_key   # 403
```

- [ ] Every response above matches. `GET /api/settings` masks secrets; nothing in `logs/` contains a key, token or password (grep for `apxr_`, `sk-`, your SMTP password).

---

## 9. Security audit follow-ups (from `security-audit/REPORT.md`)

Known, documented and accepted for a single-owner deployment — re-check they still hold and nothing worse appeared:

- [ ] `/api/apply/status/{id}`, ghostwriter routes, profile, search prefs and resume upload are reachable by any signed-in account (not scoped to the owner) — only matters with `ALLOW_SIGNUP=true`.
- [ ] `/api/apply/limits` and the daily cap count applications across all accounts.
- [ ] Session cookie: HttpOnly, SameSite=Lax, Secure unless `COOKIE_SECURE=false`.
- [ ] Login throttle (5 per account / 20 per IP per 15 min) and change-password counting toward it.
- [ ] Custom provider base URL refuses link-local/metadata addresses (loopback and LAN are allowed for local model servers).
- [ ] Runner uploads: PNG signature checked, 3 MB max, 20 per task, server-chosen file names.

---

## 10. Final pass

- [ ] `python -m unittest discover -s tests` still passes after your manual session.
- [ ] `cd frontend && npm run build` still succeeds.
- [ ] One Ctrl+C stops the server cleanly; `data/` contains no plaintext keys (`grep -r "apxr_\|sk-" data/` finds nothing readable; the DB values are Fernet blobs starting with `gAAAA`).
