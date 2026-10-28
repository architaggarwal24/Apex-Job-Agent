# Apex Job Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Node](https://img.shields.io/badge/node-%E2%89%A520.9-green)

A self-hosted job-search copilot. Apex finds jobs, scores them against your profile, prepares a tailored
application for each one, and fills in the forms, then **stops and shows you exactly what it filled in
before anything is submitted.** You stay in control: submission is a deliberate action unless you have
explicitly switched on auto-approval rules that the agent has to earn.

> **Heads-up:** Apex is a single-owner app. It is safe to run privately on your own machine or behind a login,
> but it is **not** ready to host for other people. See [Security and multi-user status](#security-and-multi-user-status).

## Contents

- [What it does](#what-it-does)
- [How an application flows](#how-an-application-flows)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Running it](#running-it)
- [Configuration](#configuration)
- [The local runner](#the-local-runner)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Security and multi-user status](#security-and-multi-user-status)
- [Responsible use](#responsible-use)
- [License](#license)

## What it does

**Find and rank jobs**

- Scrapes **LinkedIn**, **Indeed** and **Naukri** for your target titles and locations (LinkedIn and Indeed are
  limited to their Easy Apply listings), de-duplicates against what you already have, and keeps everything in
  a local SQLite database.
- An LLM scores each new job from 0 to 100 against your profile and gives a reason. Jobs below
  `AUTO_SKIP_SCORE_THRESHOLD` are skipped automatically.
- For the best matches it writes a job brief and a tailored version of your resume.

**Prepare applications**

- **Resume variants:** keep several PDFs ("Backend", "Data", "Management"), each tagged. The agent picks the best
  one per job.
- **Application kits:** for each job, a chosen resume, a short tailored summary and a cover letter, versioned so
  you can regenerate or edit them.
- **Answer bank:** every question an application form has asked you, and the answer you gave, so the same question
  is answered the same way next time. Standard questions (name, email, phone, links) are answered from your
  profile without calling an LLM.

**Apply, with you in the loop**

- A background **queue worker** works through the jobs you approve, highest score first, one at a time. It never
  submits on its own.
- **Site adapters** for Greenhouse and Lever, plus a **universal form filler** that uses the LLM to read and fill
  any other HTML application form. Site type detection also recognises Workday, Ashby and SmartRecruiters, so
  reliability is tracked per site.
- A **review packet** for every application lists each field the agent saw, what it put there, where that value
  came from (profile, answer bank, or an AI guess), how confident it is, and what to double-check. Correct
  anything, then approve or reject.
- **Apply modes:** `auto` (the agent drives the form) or `assist` (the agent prepares a copy-ready answer pack and
  you apply on the site yourself, then record it). Out of the box LinkedIn and Indeed are on `assist` and
  everything else is on `auto`; both are configurable per site type.
- **Pre-flight checks** run before any browser is opened, and every failure is sorted into one of nine classes
  (CAPTCHA, login required, unsupported form, unanswered question, closed job and so on) so the job lands in
  **Needs you** with a reason instead of failing silently.
- **Autonomy (off by default):** let the agent approve applications itself, but only for site types where a
  measured record shows it has earned it (minimum score, minimum number of human-reviewed attempts, success rate,
  every field sourced from your answer bank or profile, a per-day cap). Any failure on a site type switches its
  rules off until you turn them back on.

**Track the whole hunt**

- One lifecycle for every job:
  `found → queued → preparing ⇄ needs_you → ready_for_review → submitted → recruiter_screen → technical_round →
  hiring_manager → offer`, with `rejected`, `withdrawn`, `skipped` and `failed` as terminal states. Every move is
  recorded on a timeline and streamed live to the UI.
- **Ghostwriter:** a per-job chat assistant that knows the job description, your profile and your tailored resume.
  It drafts cover letters, likely interview questions with suggested answers, and screening-question responses.
- **Follow-ups:** reminders with a drafted message. Apex never sends one for you.
- **Interview and offer prep:** generated in the background when a card reaches a technical round, a hiring-manager
  round or an offer, plus a place to record the components of offers you receive (base, variable, bonus, equity).
- **Insights:** how your applications actually turned out, and what that says about your score threshold.
- **Notifications** by e-mail (SMTP) and/or Telegram when the agent needs you, a review is ready, a follow-up is
  due or an interview is coming up. Digests, rate limits and quiet hours are built in.

**LLM providers:** NVIDIA NIM, OpenRouter, Google Gemini, Ollama (local), Anthropic and OpenAI. Pick one with
`LLM_PROVIDER` or in Settings.

## How an application flows

```text
scrape ─▶ score ─▶ you approve ─▶ queue worker prepares it ─▶ review packet ─▶ you approve ─▶ submit
 LinkedIn   0-100   (or auto-skip)   kit + form filled, no submit   check every field    (or an autonomy rule
 Indeed                                                                                    that has earned it)
 Naukri
                          anything blocked ─▶ "Needs you", with the reason
```

## Requirements

- **Python 3.10 or newer** (developed and tested on 3.12)
- **Node.js 20.9+**, only to build the web UI once (it is not needed at runtime)
- **Opera** (a Chromium-based browser). The agent drives your real, already-logged-in Opera profile, which is why
  no job-site passwords are ever stored. The example paths in `.env.example` are for Windows; on other systems
  point `OPERA_EXECUTABLE_PATH` and `OPERA_USER_DATA_DIR` at your own Opera install and profile.
- An API key for one LLM provider, or a local [Ollama](https://ollama.com) install

## Quick start

```bash
# 1. Get the code
git clone https://github.com/architaggarwal24/Apex-Job-Agent.git
cd Apex-Job-Agent

# 2. Python environment
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install

# 3. Configure
cp .env.example .env                # then edit it (see Configuration)

# 4. Build the web UI once
cd frontend && npm install && npm run build && cd ..

# 5. Run
python main.py
```

Open <http://localhost:8000> and **create your account. The first account becomes the owner.** Then follow the
get-started checklist on the Today page: upload a resume, set the job titles and locations to search, and run a
search.

Things worth doing in `.env` before the first run:

- Set `LLM_PROVIDER` and the matching API key.
- Set `COOKIE_SECURE=false` while you are on plain `http://localhost`. Leave it `true` for any real deployment.
- Set `OPERA_EXECUTABLE_PATH` and `OPERA_USER_DATA_DIR`, and **close Opera** before the agent starts (it opens
  your profile).
- Consider `DRY_RUN=true` for your first few runs so nothing is ever actually submitted.

The first time a job site needs a login, the agent opens its login page and waits (up to 90 seconds) for you to
sign in manually. The session is then kept, encrypted at rest.

## Running it

| Command | What it does |
|---|---|
| `python main.py` | Start the web UI and API (default) at `http://localhost:8000` |
| `python main.py --dev-ui` | Also start the Next.js dev server with hot reload; open `http://localhost:3000` |
| `python main.py --pipeline` | Run the search and scoring pipeline once, then exit |
| `python main.py --pipeline --quick` | Same, in quick mode (2 titles x 2 locations) |
| `python main.py --schedule` | Web UI plus a daily scheduled pipeline run |
| `python main.py --parse` | Parse `resume.pdf` from the project root and seed the profile |

Ctrl+C stops running background work cleanly. Press it twice to force-exit.

In production-style use the UI is a static export (`frontend/out/`) served by FastAPI, so a single process and
a single origin is all you need; the browser never talks to anything else, so there is no CORS to configure. See
[`frontend/README.md`](frontend/README.md) for how the two halves fit together.

The UI has five pages: **Today** (get-started checklist and what needs you), **Applications** (board, list and
review queue, with a side panel per application), **Insights**, **Profile** and **Settings**.

## Configuration

Everything comes from `.env` (copy `.env.example`). Many of these can also be changed from the Settings page,
where API keys are stored encrypted.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `nvidia` | `nvidia`, `openrouter`, `gemini`, `ollama`, `anthropic` or `openai` |
| `LLM_TEMPERATURE` | `0.1` | Sampling temperature |
| `NVIDIA_NIM_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | Key for the provider you use |
| `*_MODEL` (for example `ANTHROPIC_MODEL`) | see `.env.example` | Model name per provider |
| `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | `qwen3:8b`, `http://localhost:11434/v1` | Local Ollama. The base URL must end in `/v1` |
| `OPERA_EXECUTABLE_PATH`, `OPERA_USER_DATA_DIR` | | Your Opera binary and profile |
| `BROWSER_HEADLESS`, `BROWSER_SLOW_MO` | `false`, `60` | Browser behaviour |
| `APP_HOST`, `APP_PORT` | `127.0.0.1`, `8000` | Where the server listens |
| `APP_SECRET_KEY` | auto-generated | Session signing key (32+ random characters if you set it) |
| `APP_ENCRYPTION_KEY` | auto-generated | Fernet key that encrypts API keys and saved site sessions |
| `COOKIE_SECURE` | `true` | Send the session cookie over HTTPS only. Set `false` only for plain-http local use |
| `ALLOW_SIGNUP` | `false` | Leave off. See multi-user status below |
| `DRY_RUN` | `false` | Go through the motions without submitting anything |
| `DAILY_APPLY_LIMIT` | `30` | Maximum applications per day |
| `AUTO_SKIP_SCORE_THRESHOLD` | `30` | Auto-skip jobs scoring below this (`0` keeps everything) |
| `MIN_DELAY_BETWEEN_APPLIES`, `MAX_DELAY_BETWEEN_APPLIES` | `45`, `120` | Random pause (seconds) between applications |
| `SCHEDULE_TIME`, `TIMEZONE` | `09:00`, `Asia/Kolkata` | Time and zone of the daily run with `--schedule` |

If you leave the two keys unset, Apex generates `data/.session_secret` and `data/.master.key`.
**Back up `data/.master.key`:** if you lose it, encrypted data cannot be recovered. For a real deployment, set
`APP_ENCRYPTION_KEY` from your host's secret store instead.

## The local runner

By default the server does the browser work itself. The **runner** is the alternative: a small program on your own
computer that does an application's browser work in *your* browser profile, so CAPTCHAs and logins happen in a
window you can see, and the server never holds your job-site sessions.

1. In Settings, the owner creates a **runner token** (it starts with `apxr_` and is shown once).
2. On the computer that has your browser, from a checkout of this project with `requirements.txt` installed:

   ```bash
   python runner/runner.py --server https://apex.example.com --token apxr_... [--profile-dir PATH]
   ```

The runner claims a task, opens a visible browser window, fills the form, streams progress, uploads screenshots
and posts the result. It never reads, forwards or posts a cookie or password, authenticates with the bearer token
only, and does not follow redirects. On a CAPTCHA or login wall it brings the window forward and waits for you.
If you press Ctrl+C mid-task it posts no result, deliberately: a half-finished pass may or may not have clicked
Submit, so the server's own rules decide what happens next.

## Project layout

```text
apex_agent/
├── main.py                 entry point (web UI, pipeline, scheduler, resume parsing)
├── core/                   config, auth, crypto, LLM client, browser, notifier, throttle, shutdown
├── db/                     SQLite stores: jobs, lifecycle, profile, settings, users, resumes, kits,
│                           review, answer bank, autonomy, follow-ups, prep, outcomes, runner
├── scraper/                linkedin.py, indeed.py, naukri.py
├── intelligence/           scorer, briefer, ghostwriter, follow-ups, interview/offer prep
├── applier/                queue worker, session orchestrator, universal form filler, review packets,
│   │                       autonomy, apply modes, preflight, failure classes, kits, answer pack
│   └── adapters/           Greenhouse and Lever, on a shared declarative form adapter
├── runner/                 the local runner (runs on your computer)
├── tracking/               pipeline.py: scrape → dedupe → score → brief
├── server/                 FastAPI app, routes/, WebSocket and SSE, static shell for the UI
├── frontend/               Next.js 16 + React 19 UI (static export served by FastAPI)
├── templates/              legacy setup page
├── tests/                  unittest suite and fixtures
├── MULTI_USER_STATUS.md    what is and is not safe to share
└── .env.example
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite is plain `unittest` (about 1,500 tests) and uses real throwaway SQLite files rather than mocks. The
HTTP-layer tests (auth gate, routes, WebSocket) need FastAPI and skip themselves without it, so run
`pip install -r requirements.txt` first. If one of those fails, believe the test first.

## Security and multi-user status

Apex is built to run as a **private, single-owner instance**.

- Passwords are hashed with scrypt; sessions are signed, expiring, `HttpOnly` and `SameSite=Lax` cookies; every
  route sits behind a login gate; login attempts are throttled.
- API keys and saved job-site sessions are encrypted at rest. **No job-site password is stored anywhere.**
- Sign-up is closed after the first (owner) account. Do not set `ALLOW_SIGNUP=true`: the profile, settings and
  parts of the apply flow are still global, so a second account would see the owner's data.
- Behind a reverse proxy, start uvicorn with `--proxy-headers` and `--forwarded-allow-ips=<proxy ip>`, and keep
  `APP_HOST=127.0.0.1` so the proxy is the only way in. The login throttle is in-memory and per-process.

[`MULTI_USER_STATUS.md`](MULTI_USER_STATUS.md) has the full list of what is done, what blocks opening sign-ups,
and the threat model.

## Responsible use

Automating applications can break the terms of service of LinkedIn, Indeed and other job sites, and an account can
be restricted or suspended as a result. That is why LinkedIn and Indeed default to `assist` mode. Use Apex on your
own accounts, keep the daily limit modest, review what it fills in, and take responsibility for what you submit.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Archit Aggarwal.
