# Apex Job Agent — security audit report (run-2)

## 1. Scope and method

- **Profile:** `standard`, full repository scope (`.`), no agent budget set. Agents spent: 4 reconnaissance, 16 hunters (incl. 2 re-assignments), 3 coverage critics, 16 candidate verifiers, 16 final record verifiers.
- **Source ref:** no VCS in the delivered tree; audited a frozen read-only copy of the working tree after the stage-1 shutdown/crypto fix (195 files, aggregate sha256 `19a1baf588fa65de`). `frontend/node_modules`, `.next`, `out`, `data/`, `logs/` excluded.
- **Execution policy:** sandboxed source-and-local-only. Every target-controlled run happened inside an OS-enforced sandbox (mount/net/pid namespaces, all mounts read-only except a per-agent scratch tmpfs, uid `nobody`, empty environment, prlimit 1 GiB / 128 procs / 20 MiB files / 120 s CPU, loopback only). Dummy accounts, dummy secrets and stubbed LLM calls only. No deployed endpoint, provider, job site or mail/Telegram service was contacted.
- **Prior runs:** `run-1` contained only an abandoned sandbox setup — no ledger and no findings — so nothing was carried over and no exclusions applied.
- **Coverage:** 23 ledger units, all closed (`covered` or `candidate`); no `deferred`, `blocked` or `out_of_scope` units. Three post-wave coverage critics ran; the final-clean critic accepted no further work (`stop: true`). One hunter run aborted before doing anything (its instruction read was blocked by a tool safety check) and the unit was re-assigned to a fresh hunter; one verifier and one hunter were interrupted by an API usage limit and resumed. This is one pass, not proof of absence.

## 2. Posture summary

The core controls are in good shape: one auth gate in front of every route, scrypt passwords with constant-time compare, session-version revocation on password change / sign-out-everywhere, SameSite=Lax + HttpOnly cookies with no CORS, parameterised SQL throughout, React-escaped rendering with `safeHref` on every external link, Fernet encryption of API keys / cookies / resumes, hashed 256-bit runner tokens with an exact-path gate bypass, and SSRF checks on provider and SMTP hosts. No Critical or High finding survived validation.

What the audit did find clusters in three places:

1. **The owner boundary is incomplete for a second account.** Several routes that touch machine-wide owner data (profile, `resume.pdf`, search preferences, apply-session JSON, screenshots, ghostwriter, generic settings reads, `/api/apply/limits`) are protected only by the session gate. All of these require `ALLOW_SIGNUP=true` (off by default) and are already listed in `MULTI_USER_STATUS.md` as blockers for multi-user use. Four Mediums and three Lows.
2. **The LLM-to-action safeguards have gaps.** Answers typed for one application overwrite master profile facts under a key the job page chose (Medium); failed scoring is stored as 50 and passes the quality gate (Low); the runner executor lets autonomy exceed the daily cap (Low); the approved submit re-reads the live form (Low).
3. **Runner and dev-mode edges.** A `wait=NaN` claim never times out and survives token revocation (Medium); in `--dev-ui` the login throttle can be bypassed with a forged `X-Forwarded-For` (Medium).

Two further leads (unbounded multipart spooling on the public runner screenshot route; unbounded PDF decompression on resume upload) were reproduced locally but their real-world impact depends on deployment facts that source cannot show — see section 5.

## 3. Confirmed findings

| # | Severity | Title | Boundary | Observed result (local, dummy data) |
|---|---|---|---|---|
| 1 | **medium** | A runner claim with wait=NaN never times out, spins the shared jobs DB, and still delivers a task after the token is revoked | `server/routes/runner.py → db/runner_db.py` | Independent re-run by the verifier. The wait=1 baseline returned {"task":null} after 1.01 s, and wait=1e9 was clamped and returned after 25.02 s. The NaN poll was still open after … |
| 2 | **medium** | Answers to form questions overwrite the owner's master work-authorization, sponsorship and identity facts under keys the job page chose | `applier/adapters/form.py → applier/session.py` | V1: both fields have key authorized_to_work / require_sponsorship, db_value '' and needs_manual True. V2: the prompts carry those labels with those keys. V3: the profile changed fr… |
| 3 | **medium** | Any signed-in user can read the owner's apply session JSON and filled-form screenshots | `server/routes/apply.py → server/app.py` | The computed id 4736df4f1bef05c6 equals the owner's job id. As user 2, GET /api/apply/status/4736df4f1bef05c6 returned 200 with the owner's session. It contained application_answer… |
| 4 | **medium** | Ghostwriter routes let any signed-in non-owner read the owner's job data and profile PII and spend the owner's LLM key | `server/routes/ghostwriter.py → server/routes/ghostwriter.py` | User 2 got 404 from GET /api/jobs/<owner job> and 403 from POST /api/kits/<owner job>/regenerate. GET /api/ghostwriter/context/<owner job> returned 200 with the owner's job title, … |
| 5 | **medium** | In --dev-ui mode, a client-supplied X-Forwarded-For passes through the loopback Next proxy and lets anyone bypass the login brute-force throttle | `frontend/next.config.mjs → core/throttle.py` | uvicorn 0.54.0, config.proxy_headers=True, forwarded_allow_ips='127.0.0.1,::1'. The control without XFF through the proxy returned [401,401,401,401,401,429,429], so the throttle wo… |
| 6 | **medium** | Non-owner accounts can read and overwrite the owner's global profile, resume.pdf and search preferences | `server/routes/onboarding.py → applier/session.py` | Bob's GET /api/profile/flat returned 200 with email owner-v07@example.com and phone +10000000777. GET /api/profile and /api/status also returned 200. Bob's POST /api/profile return… |
| 7 | **low** | /api/setup/upload-resume accepts uploads of any size and runs PDF parsing and the LLM call on the server's single event loop, stalling every other request | `server/routes/onboarding.py → core/resume_parser.py` | 12 MiB upload: 200 {"status":"ok"}, RESUME_PATH = 12,582,921 bytes, equal to the bytes sent. 60-page PDF upload: 200 in 4.3 s; probe on_event_loop_thread=true, extract_seconds=3.63… |
| 8 | **low** | An open SSE event stream (and WebSocket) keeps sending a user's job events after their sessions are revoked | `server/routes/applications.py → server/sse.py` | logout-everywhere returned 200 {"status":"ok"}. With the old cookie, /api/stats and a new /api/events/stream both returned 401 {"detail":"Not logged in"}. The stream opened before … |
| 9 | **low** | Approved submit re-reads the live form and submits field values that were never in the reviewed or auto-approved packet | `server/routes/review.py → applier/universal.py` | answers_known(packet)=true and rule_failures=[]. Variant A: the real execute() typed '1990-01-01' into #dob and 'yes' into #share. Neither field is in the packet. The real click_su… |
| 10 | **low** | Autonomy auto-submits past the daily apply limit when the runner executor is used | `applier/queue_worker.py → applier/runner_hub.py` | Scenario A: effective_daily_limit=2; worker outcomes processed x5 then idle; runner log shows apply x5 (applied_today 0 before and after each), then submit x5 (applied_today 0->1->… |
| 11 | **low** | Failed LLM scoring is stored as a real score of 50, so it passes the quality gate and autonomy min_score rules | `server/routes/jobs.py → applier/session.py` | In all 5 failure modes the stored ai_score was 50 and the job stayed in stage found. The reason was 'Scoring failed — kept for manual review', except for the missing-key mode, whic… |
| 12 | **low** | GET /api/apply/limits shows non-owner users the owner's application count and last-apply timing | `server/routes/apply.py → server/routes/apply.py` | bob before: /api/apply/limits applied_today 0, wait_seconds 0. After the owner's three submissions, bob's GET /api/apply/limits returns 200 {daily_limit:10, applied_today:3, daily_… |
| 13 | **low** | Generic settings reads show the owner's private notification addresses and the last 4 characters of secrets to non-owners | `server/routes/settings.py → server/routes/settings.py` | User 2: GET /api/notifications/settings returned 403 (the owner got 200). GET /api/settings returned 200 with notify_email_to='owner-inbox-dummy@example.com', notify_telegram_chat_… |

## 4. Finding details

### 1. A runner claim with wait=NaN never times out, spins the shared jobs DB, and still delivers a task after the token is revoked

- **Fingerprint:** `applier/runner_hub.py:RunnerHub.claim:nan-wait-unbounded-long-poll-survives-token-revocation`  
- **Severity:** medium (likelihood low: The attack needs a runner token, and only the owner can mint one. The realistic attacker has a stolen or leaked token or controls a compromised runner host. With a token, the attack is one trivial request. — impact medium: It defeats token revocation with no time limit. After revocation, the revoked holder receives the owner's profile subset, answer bank, application answers and kit for the next queued runner task, and that task is kept from the legitimate runner until the lease lapses. The resume bytes stay protected, because downloading them needs a fresh, now-rejected request. Each spinning request also holds a worker thread and runs about 3,000 write transactions per second on the shared jobs DB until a task arrives.)  
- **Confidence:** high — Every trace line was re-read and matches the source. The verifier reproduced the result independently in the sandbox through the real FastAPI app (TestClient): a poll with no time limit, 429 on a second poll, 401 on fresh requests after revocation, and a 200 payload delivered with the task row claimed by the revoked token id.

**What it is.** POST /api/runner/claim takes a 'wait' value from the client. RunnerHub.claim clamps it with min(max(float(wait), 0.0), 25.0), but float('nan') gets through that clamp unchanged, so the deadline and the remaining time are both NaN. The exit check 'remaining <= 0' is never true. _wait(min(NaN, 1.0)) calls threading.Condition.wait(NaN). Because 'NaN > 0' is false, Condition.wait takes the non-blocking acquire path and returns at once. The long poll becomes a busy loop with no time limit: about 3,000 sweep()+claim() iterations per second, each one a BEGIN IMMEDIATE write transaction on the shared jobs database. The loop holds one default-executor thread and the token's single polling slot, so later polls by the same token get 429. The token is checked only once, when the request starts (require_runner -> authenticate). When the owner revokes the token, fresh requests get 401, but the open poll keeps running. The revoke route only sets revoked_at, and RunnerTaskStore.claim never checks whether the token is revoked. So the next runner task the owner queues is claimed under the revoked token id and returned to the revoked holder: the profile subset (pack.PROFILE_KEYS), the answer bank, the application answers, the kit and the resume metadata. The resume bytes themselves need a fresh request, which now gets 401. The task stays claimed by the dead token, and the legitimate runner cannot have it until the lease lapses. If it was a submit task, it then ends as lost_submit. With an ordinary numeric wait the same gap exists, but only for up to 25 s after revocation; a control run with wait=1e9 was clamped and returned after 25.02 s.

**Root cause.** RunnerHub.claim (applier/runner_hub.py:296) does not reject non-finite wait values. Its loop (lines 305-317) ends only when 'remaining <= 0', which is never true for NaN, and Condition.wait(NaN) does not block. Token liveness is checked only once, at request start (server/routes/runner.py:40 -> core/runner_auth.py:46 -> db/runner_db.py:171). RunnerTaskStore.claim (db/runner_db.py:297-309) hands a task to any token_id without checking that the token is still unrevoked. RunnerTokenStore.revoke (db/runner_db.py:215-224), the only thing the DELETE route calls (server/routes/runner_tokens.py:41), does not end open polls or release tasks the token has claimed.

**Intended behaviour.** A claim waits at most MAX_WAIT_SECONDS (25 s). A non-numeric or non-finite wait gets a 400. The documented invariant (db/runner_db.py:15, server/routes/runner_tokens.py:8) is that a revoked token stops working at once. So a revoked token, or a token whose user is inactive, can never claim a task or receive a payload after the revocation commits, including from a poll that started earlier.

**Source path.**

- `server/routes/runner.py:51` (entrypoint, claim) — POST /api/runner/claim accepts the JSON body as a plain dict. Line 52 takes wait = data.get('wait') and passes it to RunnerHub.claim in a worker thread without validating it.
- `server/routes/runner.py:40` (propagation, require_runner) — The bearer token is authenticated once, at request start. Nothing re-checks it during the long poll.
- `applier/runner_hub.py:296` (propagation, RunnerHub.claim) — wait = min(max(float(wait), 0.0), 25.0). float('nan') succeeds, and both max and min keep NaN.
- `applier/runner_hub.py:305` (propagation, RunnerHub.claim) — deadline = clock() + NaN. At lines 311-314, remaining is NaN, 'remaining <= 0' is false, and _wait(min(NaN, 1.0)) -> Condition.wait(NaN) returns at once. The result is a busy loop with no bound, calling sweep() and _claim_valid().
- `db/runner_db.py:215` (propagation, RunnerTokenStore.revoke) — Revocation only sets revoked_at. Open polls and tasks already claimed by that token are left as they are.
- `db/runner_db.py:306` (sink, RunnerTaskStore.claim) — UPDATE runner_tasks SET status='claimed', claimed_by=<revoked token_id> with no check of runner_tokens.revoked_at. RunnerHub.payload (applier/runner_hub.py:378-389) then returns the profile subset, answers and kit to the revoked holder.

**Lower-trust principal.** Holder of a runner token that the owner later revokes

**Conditions.**

- *authentication_level*: The attacker holds a runner bearer token that was valid when the claim request started, for example a stolen or leaked apxr_ token or a compromised runner machine. Only the owner can mint tokens.
- *data_state*: For the disclosure after revocation, the owner must queue a runner task (executor runner, or auto) while the spinning poll is alive. The availability effect (busy loop, a held thread, a write transaction on every iteration) needs no data.
- *network_routing*: To read the payload, the attacker keeps the HTTP connection open until a task arrives. The server-side spin and the claim of the task under the revoked token happen even if the client disconnects, because asyncio.to_thread work is not cancelled.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `POST /api/runner/claim with header 'Authorization: Bearer <apxr_ token>' and body {"wait": "nan"}`
- Copy the source into sandbox scratch and make the copy writable. In-process, set up the app the way tests/test_runner.py RunnerHttpTests does: temp settings/users/jobs DBs, a signup for the dummy owner, then the owner creates a runner token through POST /api/settings/runner-tokens.
- Baseline: POST /api/runner/claim {"wait":1} returns {"task":null} after about 1 s. Control: {"wait":1e9} is clamped and returns after about 25 s.
- From a thread, POST /api/runner/claim {"wait":"nan"} with the token, counting RunnerTaskStore.sweep calls.
- After 27 s, check that the request is still open and that a second claim with the same token gets 429.
- As the owner, DELETE /api/settings/runner-tokens/<id>. Check that a fresh claim with the token now gets 401 and that the NaN poll is still alive.
- Add a dummy job, move it to preparing, and enqueue an apply task with RunnerHub(1).enqueue. Check the pending response and the task row's claimed_by.
- Command: sbx.sh <scratch> 120 bash -c 'cd /mnt/work/src && env APP_ENCRYPTION_KEY=<fresh Fernet key> APP_SECRET_KEY=<40+ char dummy> python3 /mnt/work/v02_nan.py'. The output is written to scratch work/evidence/v02_nan_revoked.json, a predeclared file for parent promotion.

**Observed:** Independent re-run by the verifier. The wait=1 baseline returned {"task":null} after 1.01 s, and wait=1e9 was clamped and returned after 25.02 s. The NaN poll was still open after 27 s, with 80727 sweep() calls. A second poll by the same token got 429 'this token is already waiting for a task'. Revoke returned 200, a fresh claim with the token got 401, and the NaN poll was still alive 2 s later. After an apply task was queued, the NaN poll returned HTTP 200 after 29.06 s carrying that task's id and the payload keys [answers, application, attempt, kind, kit, lease_seconds, profile, resume, task_id]. The task row showed status=claimed, claimed_by=1, which is the revoked token's id, and the token list showed active=false.

**Smallest fix.** Reject a non-finite wait with a 400. Re-check token liveness on every loop iteration. Make the claim UPDATE itself require an unrevoked token, so a revocation committed during a poll cannot hand out a task. On revoke, return tasks claimed by that token to the queue; submit tasks go to the existing lost_submit handling instead.

`applier/runner_hub.py`:
```python
import math
...
    def claim(self, token_id: int, wait: float = MAX_WAIT_SECONDS) -> dict | None:
        try:
            wait = float(wait)
        except (TypeError, ValueError):
            raise RunnerError("wait must be a number of seconds")
        if not math.isfinite(wait):
            raise RunnerError("wait must be a finite number of seconds")
        wait = min(max(wait, 0.0), float(MAX_WAIT_SECONDS))
        ...
            while True:
                if not self.tokens.is_live(token_id):   # revoked or user deactivated since the request began
                    raise RunnerError("this runner token was revoked")  # status 401
                self.sweep()
                ...
```
`db/runner_db.py`:
```python
            c.execute("UPDATE runner_tasks SET status='claimed', claimed_by=?, claimed_at=?, lease_until=?, "
                      "attempts=attempts+1 WHERE id=? AND user_id=? AND status='queued' "
                      "AND EXISTS (SELECT 1 FROM runner_tokens t WHERE t.id=? AND t.user_id=? AND t.revoked_at=0)",
                      (token_id, now, now + lease, r["id"], self.user_id, token_id, self.user_id))
            # return None when rowcount == 0
```

### 2. Answers to form questions overwrite the owner's master work-authorization, sponsorship and identity facts under keys the job page chose

- **Fingerprint:** `applier/session.py:_save_answers:page-derived-field-key-writes-master-profile-bypassing-CORRECTABLE_FIELDS`  
- **Severity:** medium (likelihood medium: Foreign-country work-authorization and sponsorship questions are common on ATS forms. The deterministic adapter path needs no attacker sophistication: answering them truthfully with the default Remember setting triggers the overwrite. The LLM-key variant also needs the page to steer the model. — impact medium: The owner's legally sensitive work-authorization and sponsorship facts, or identity fields, are durably changed. Later real applications to other employers are then filled with false values without asking, and review shows them as ordinary profile facts. The impact is limited to the integrity of submissions made in the owner's name. There is no confidentiality or cross-tenant effect.)  
- **Confidence:** high — The verifier reproduced the result independently in the sandbox through the real GreenhouseAdapter._field, standard.resolve, Orchestrator._finish_first_pass, Orchestrator.run_iter2 and ProfileDB code with dummy data. The corrections-path control shows the allow-list is skipped only on this path. Only the LLM-key variant rests on source reading, and the deterministic adapter path needs no LLM.

**What it is.** When an application stops on a mandatory question, the owner sees only the page's label (ApplyTab.jsx:88) and types an answer. Orchestrator._save_answers (applier/session.py:298) and ProfileDB.resolve_missing (db/profile_db.py:347) then write that answer into the single machine-wide profile under the prompt's field_key. There is no allow-list. The key comes from the untrusted application form. The site adapters map labels by regex: standard.key_for turns any work-authorization label, including one about another country, into authorized_to_work, and any sponsorship label into require_sponsorship. The universal filler takes field_key verbatim from LLM output built from page text (form_analyzer.py:299). standard.profile_value deliberately refuses to answer a question that names another country from the home-country fact, so the question goes to the owner. The owner's truthful answer to 'Are you legally authorized to work in the United States?' (No) then replaces the owner's India work-authorization fact. The 'Remember this answer' checkbox is on by default. Every later application is then auto-filled from the overwritten fact without asking, and the review packet labels it source=profile at 0.95 confidence (review.py:46,131). The same unfiltered sink accepts any key, so an LLM-assigned key (for example email or full_name on an innocuous question) overwrites that master field too. CORRECTABLE_FIELDS (db/profile_db.py:91) exists to keep identity and compliance keys from being rewritten by per-job input, but only the corrections path applies it (session.py:833).

**Root cause.** _save_answers (applier/session.py:298) calls profile_db.set_many on every answered key whose mode is not 'once', and resolve_missing (db/profile_db.py:347) calls set on the stored field_key. Neither checks the key against CORRECTABLE_FIELDS or any other allow-list. Neither ties the key's profile-wide meaning to the specific question the owner answered, even though that key was derived from page-controlled form content (adapters/standard.key_for or the LLM's field_key).

**Intended behaviour.** An answer given for one application is stored for that application (application_answers) and in the answer bank under the visible question. It reaches the master profile only for allow-listed, application-neutral keys. Identity and compliance keys (authorized_to_work, require_sponsorship, full_name, email, URLs, search_*, cover_letter, resume_summary) change only via the Profile page, as the comment at db/profile_db.py:69-90 states.

**Source path.**

- `applier/adapters/form.py:292` (entrypoint, FormAdapter._field) — The label from the page's form control becomes field_key through _key -> standard.key_for (form.py:271-275). On the universal path the LLM's field_key is copied verbatim (form_analyzer.py:299).
- `applier/adapters/standard.py:86` (propagation, key_for) — Any label matching _AUTH (line 45), including one naming the United States, returns authorized_to_work. A label matching 'sponsor' returns require_sponsorship (line 88).
- `applier/adapters/standard.py:125` (propagation, profile_value) — A question that names a country other than location_country is deliberately not answered from the profile, so resolve() marks the required field needs_manual (line 149).
- `applier/session.py:202` (propagation, Orchestrator._finish_first_pass) — flag_missing stores the page-derived field_key, and missing_prompts carry it to the UI (line 219). The UI shows only the label (ApplyTab.jsx:88).
- `server/routes/apply.py:146` (propagation, answer) — The owner's POST /api/apply/answer/{job_id} (route at line 118) runs Orchestrator.run_iter2, which calls _save_answers first (session.py:252), before preflight or any browser work.
- `db/profile_db.py:347` (propagation, ProfileDB.resolve_missing) — Also writes the value into profile_fields under the stored page-derived field_key, with source user_prompt.
- `applier/session.py:298` (sink, Orchestrator._save_answers) — set_many writes every answered key into the global profile.db with no CORRECTABLE_FIELDS filter, unlike _apply_corrections (session.py:833).

**Lower-trust principal.** Untrusted application-form content: any job form that asks a required question about another country's work authorization or sponsorship, or, through the LLM path, one whose benign question gets mapped to an identity key. The owner's own answer is then durably written over a master profile fact the owner did not choose to change.

**Conditions.**

- *authentication_level*: The owner answers a missing-field prompt for one of their applications (POST /api/apply/answer requires require_owner).
- *user_interaction*: The owner truthfully answers the question shown (for example, a US or UK work-authorization or sponsorship question) and leaves 'Remember this answer' at its default (on).
- *system_configuration*: A site adapter (Greenhouse or Lever) derives the key deterministically from the label. Or the universal filler is used and the LLM assigns an identity key to a page question.
- *data_state*: The owner's profile holds home-country values (for example location_country=India, authorized_to_work=Yes, require_sponsorship=No).

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `Required Greenhouse select label: "Are you legally authorized to work in the United States? *" (options Yes/No) -> field_key authorized_to_work; owner answer {"authorized_to_work": "No"}`
- Input: `Required Greenhouse select label: "Will you require visa sponsorship to work in the United Kingdom?" -> field_key require_sponsorship; owner answer {"require_sponsorship": "Yes"}`
- Copy the source into the sandbox scratch (work/src, owner-writable, data/ removed). Seed a dummy ProfileDB with location_country=India, authorized_to_work=Yes, require_sponsorship=No, email=owner@example.invalid.
- Build the two controls with GreenhouseAdapter()._field and run standard.resolve against ProfileDB().summary(). Both come back with empty db_value and needs_manual=True, under the keys authorized_to_work and require_sponsorship.
- Add a dummy job with JobsDB(user_id=1).add. Monkeypatch Orchestrator._preflight to return False so nothing reaches a browser. Call Orchestrator(job)._finish_first_pass with those missing_fields, then Orchestrator(job).run_iter2({'authorized_to_work':'No','require_sponsorship':'Yes'}, remember=None), which is the call the /api/apply/answer route makes.
- Read ProfileDB and field_history. Then resolve a new form with 'Are you authorized to work in India?' and 'Do you require visa sponsorship?'. As a control, call _apply_corrections with core.llm.chat_json stubbed to return authorized_to_work, email and phone.
- Run command: sbx.sh <v12 scratch> 120 bash -c 'cd /mnt/work/src && env APP_ENCRYPTION_KEY=<fresh Fernet key> APP_SECRET_KEY=<40-char dummy> python3 /mnt/work/v12_harness.py'. Output was written to scratch work/evidence/v12_answer_key_profile_overwrite.txt, which is predeclared for promotion.

**Observed:** V1: both fields have key authorized_to_work / require_sponsorship, db_value '' and needs_manual True. V2: the prompts carry those labels with those keys. V3: the profile changed from authorized_to_work=Yes, require_sponsorship=No to authorized_to_work=No, require_sponsorship=Yes. V4: field_history has ('authorized_to_work','Yes','No','user_prompt') and ('require_sponsorship','No','Yes','user_prompt'). V5: the later India form was auto-filled with 'Are you authorized to work in India?' -> 'No' and 'Do you require visa sponsorship?' -> 'Yes', both needs_manual False. V6: the corrections-path control dropped authorized_to_work and email and set only phone. The LLM-assigned-key variant (for example email) was not run. It follows from the same unfiltered set_many and resolve_missing code.

**Smallest fix.** Never let a per-application answer rewrite a master profile key unless the key is allow-listed. Write the profile only for keys that are pending prompts of this application and are in CORRECTABLE_FIELDS (or a dedicated ANSWERABLE set). Keep every other answer only in application_answers and the label-keyed answer bank, and pass store=False to resolve_missing for it. Add regression tests: answering a US-authorization prompt keyed authorized_to_work leaves profile.authorized_to_work unchanged, and an LLM-keyed 'email' prompt leaves profile.email unchanged.

`applier/session.py`:
```python
    def _save_answers(self, answers: dict[str, str], modes: dict[str, str]):
        from db.profile_db import CORRECTABLE_FIELDS
        s = self.session
        prompt_keys = {p["field_key"] for p in s.missing_prompts}
        def to_profile(k):
            return k in prompt_keys and k in CORRECTABLE_FIELDS and modes.get(k) != "once"
        self.profile_db.set_many({k: v for k, v in answers.items() if to_profile(k)},
                                 source="user_prompt")
        bank = self._bank()
        for p in s.missing_prompts:
            key, value = p["field_key"], (answers.get(p["field_key"]) or "")
            if not value:
                continue
            mode = modes.get(key, "auto")
            confirmed = s.bank_confirm.pop(key, None)
            s.application_answers[normalize_question(p["label"])] = value
            self.profile_db.resolve_missing(p["id"], value, store=to_profile(key))
            ...  # answer-bank handling unchanged
```

### 3. Any signed-in user can read the owner's apply session JSON and filled-form screenshots

- **Fingerprint:** `server/routes/apply.py:apply-status-and-screenshots-missing-job-ownership`  
- **Severity:** medium (likelihood low: Requires ALLOW_SIGNUP=true, a second account, and knowledge of a public posting URL the owner applied to. Once the URL is known, the id needs no further guessing (for pipeline-created jobs it is not even salted by user id). — impact medium: Read-only disclosure of the owner's per-application answers, fill logs and full-page filled-form screenshots, which contain the owner's PII. No modification.)  
- **Confidence:** high — Every trace line was re-read in source, and the result was reproduced independently end to end in the sandbox with dummy principals and data. The sibling per-user routes refused the same id, so no other layer enforces ownership. The unscoped-pipeline id derivation is directly visible in db/jobs_db.py:383-386 and tracking/pipeline.py:50/110.

**What it is.** GET /api/apply/status/{job_id} loads the global data/sessions/<job_id>.json and does not check that the caller owns the job. Job ids are deterministic and can be computed offline from a public job-posting URL. Jobs created by the production pipeline (tracking/pipeline.py, which uses an unscoped JobsDB()) get id sha256(<normalized url>)[:16] with no user component; those rows are later adopted by user 1. Jobs created through a scoped JobsDB(user_id=N).add get sha256('<N>:<normalized url>')[:16]. Either way a non-owner can compute the owner's job id from the posting URL. The response is the whole session: the answers the owner typed for that application (missing-field answers are stored in application_answers, applier/session.py:307), the missing-field prompts, the fill log and the absolute screenshot paths. The /screenshots StaticFiles mount then serves those full-page filled-form PNGs to any signed-in session, with no per-user check. For the same id and the same non-owner, the per-user routes GET /api/jobs/{id} and GET /api/applications/{id}/review return 404.

**Root cause.** In server/routes/apply.py, status() declares no user dependency and never calls _get_job(job_id, uid) before ApplicationSession.load(job_id). server/app.py mounts data/screenshots as a plain StaticFiles directory, so only the global session gate protects it. Both derived copies are keyed only by job_id and are never bound to JobsDB(user_id=uid).

**Intended behaviour.** A session or screenshot is served only when JobsDB(user_id=uid).get_by_id(job_id) resolves for the caller, as /api/stats, /api/jobs/{id} and the review routes already require. Otherwise the response is 404.

**Source path.**

- `server/routes/apply.py:110` (entrypoint, status) — GET /api/apply/status/{job_id} takes the job_id from the path and has no uid dependency or ownership check. The only control on the path is the global session gate.
- `applier/session.py:96` (propagation, ApplicationSession.load) — Opens the global cfg.SESSIONS_DIR/<job_id>.json for any id, with no user scoping.
- `applier/session.py:141` (propagation, ApplicationSession.to_dict) — asdict(self) returns the whole session: application_answers, missing_prompts, log and absolute screenshot paths.
- `tracking/pipeline.py:110` (propagation, run) — The only production job writer calls add() on an unscoped JobsDB() (created at line 50), so the row is stored with user_id NULL and later adopted by user 1 (db/jobs_db.py:284).
- `db/jobs_db.py:386` (propagation, JobsDB._job_id) — The job id is sha256(normalized url)[:16] when the writer is unscoped (the pipeline), or sha256('<uid>:<normalized url>')[:16] when it is scoped. Both are deterministic and can be computed from a public posting URL.
- `server/app.py:107` (sink, create_app /screenshots mount) — StaticFiles serves any file in data/screenshots to any signed-in session, including the basename leaked by the status response.

**Lower-trust principal.** A signed-in non-owner (user id 2) who knows a public job URL the owner (user id 1) applied to

**Conditions.**

- *system_configuration*: ALLOW_SIGNUP=true (server/routes/auth.py:44), so a second account can exist.
- *authentication_level*: Any signed-in non-owner session. Anonymous callers get 401 on the API and 303 on /screenshots.
- *data_state*: The owner has an apply session for a job whose public posting URL the attacker knows or guesses.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `job_id = sha256(JobsDB._norm_url(url).encode()).hexdigest()[:16]  (pipeline-created jobs)`
- Input: `job_id = sha256(('1:' + JobsDB._norm_url('https://www.linkedin.com/jobs/view/9876543210/')).encode()).hexdigest()[:16]  (scoped add, as reproduced)`
- Input: `GET /api/apply/status/{job_id}`
- Input: `GET /screenshots/{basename of screenshots[0]}`
- Copy the source into the sandbox scratch. Point cfg.JOBS_DB_PATH, UsersDB and SettingsDB at temp files and set ALLOW_SIGNUP=True, COOKIE_SECURE=False and a dummy APP_SECRET_KEY of 32+ characters. Then build create_app(ui_dir=<nonexistent>) with TestClient, as tests/test_http_auth.py setUp does.
- Sign up an owner (id 1) and a second user (id 2) on separate clients.
- As the trusted harness, run JobsDB(user_id=1).add for a dummy LinkedIn URL. Write a dummy PNG to cfg.SCREENSHOTS_DIR/<job_id>_filled_101500.png. Save an ApplicationSession for the job with that screenshot path, application_answers {'expected salary': 'V04-DUMMY-1234'} and a dummy log line.
- As user 2, compute the id from the URL alone, then GET /api/apply/status/{id} and GET /screenshots/<basename>. As user 2, also GET /api/jobs/{id} and /api/applications/{id}/review. Anonymously, GET both the status endpoint and the screenshot.
- The script is scratch work/scripts/repro.py. Its output is predeclared for promotion at work/evidence/v04_status_screenshot_nonowner.json.

**Observed:** The computed id 4736df4f1bef05c6 equals the owner's job id. As user 2, GET /api/apply/status/4736df4f1bef05c6 returned 200 with the owner's session. It contained application_answers {'expected salary': 'V04-DUMMY-1234'}, the dummy log line and the absolute screenshot path. GET /screenshots/4736df4f1bef05c6_filled_101500.png returned 200 with exactly the dummy PNG bytes. As user 2, /api/jobs/{id} and /api/applications/{id}/review both returned 404. As the owner, /api/jobs/{id} returned 200. Anonymous callers got 401 on status and 303 on the screenshot.

**Smallest fix.** Bind every derived copy to the job owner. status() should require a session user and call _get_job(job_id, uid) before loading the session. Replace the /screenshots StaticFiles mount with a route that takes a single basename, reads the job-id prefix (16 hex characters for both the server and runner naming schemes), checks JobsDB(user_id=uid).get_by_id and serves the file only from inside SCREENSHOTS_DIR. Add a regression test that a second user gets 404 on both paths.

`server/routes/apply.py`:
```python
@router.get("/api/apply/status/{job_id}")
async def status(job_id: str, uid: int = Depends(get_current_user_id)):
    from applier.session import ApplicationSession
    _get_job(job_id, uid)
    s = ApplicationSession.load(job_id)
    if not s:
        raise HTTPException(404, "No session")
    return JSONResponse(s.to_dict())
```
`server/app.py`:
```python
# remove app.mount("/screenshots", StaticFiles(...)) and add:
@app.get("/screenshots/{name}")
async def screenshot(name: str, request: Request):
    from fastapi.responses import FileResponse
    from db.jobs_db import JobsDB
    uid = getattr(request.state, "user_id", None)
    job_id = name.split("_", 1)[0]
    if (uid is None or "/" in name or "\\" in name or not name.endswith(".png")
            or not JobsDB(user_id=uid).get_by_id(job_id)):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    path = ss_dir / name
    return FileResponse(path) if path.is_file() else JSONResponse({"detail": "Not found"}, status_code=404)
```

### 4. Ghostwriter routes let any signed-in non-owner read the owner's job data and profile PII and spend the owner's LLM key

- **Fingerprint:** `server/routes/ghostwriter.py:_get_job_and_profile:unscoped-JobsDB-global-ProfileDB-no-owner-check`  
- **Severity:** medium (likelihood low: Requires the non-default ALLOW_SIGNUP=true and a non-owner account. After that, one authenticated GET is enough, and owner job ids can be computed from public URLs. — impact medium: Discloses the owner's profile PII and other users' per-user job data across the tenant boundary, and lets a non-owner make unlimited LLM calls on the owner's key. There is no write, no tool execution and no account takeover.)  
- **Confidence:** high — Reproduced independently by the verifier through the real FastAPI app, auth gate and routes with dummy accounts and stubbed LLM calls. Sibling routes deny the same caller, and every trace line was re-read in source.

**What it is.** The four /api/ghostwriter/* routes (cover-letter, interview-prep, chat, context) check only that a session exists, through the global auth-gate middleware. None of them declares a Depends identity or owner dependency. _get_job_and_profile loads the job with an unscoped JobsDB(), which applies no user filter, and always reads the owner's single global ProfileDB().summary(). A non-owner account, which can exist only when ALLOW_SIGNUP=true, can call GET /api/ghostwriter/context/{job_id} with another user's job id. The response contains that job's title, company, location, URL, description, brief, tailored summary and skills. It also contains the owner's name, role, experience, location, education, summary, LinkedIn, GitHub and portfolio URLs. The owner's profile appears even when the caller passes their own job id. The chat, cover-letter and interview-prep routes run LLM calls with that context through core.llm.chat or chat_json, which use the single global provider configuration and key from SettingsDB, so the non-owner spends the owner's key. The same caller gets 404 from GET /api/jobs/{owner job id} and 403 from POST /api/kits/{owner job id}/regenerate. Scoped job ids are sha256('<uid>:<normalized url>')[:16], so the owner's job ids can be computed from public posting URLs.

**Root cause.** server/routes/ghostwriter.py:_get_job_and_profile (lines 27-33) builds JobsDB() without user_id. Its _and() and _where() helpers then return empty filters (db/jobs_db.py:155-159), so get_by_id matches any user's row. The function then returns the machine-wide owner profile from ProfileDB().summary(). The route handlers at lines 70-129 have no Depends(get_current_user_id) or Depends(require_owner). The only control is the global session gate, so the request runs as the owner's data and the owner's LLM credential whoever the caller is.

**Intended behaviour.** Ghostwriter should act only on the caller's own job, through JobsDB(user_id=uid). Because it reads the owner's single global profile and spends the owner's global LLM key, it should be owner-only, like kits regenerate (Depends(require_owner)). The alternative is to give non-owners no profile, as intelligence/prep.py load_owner_profile does.

**Source path.**

- `server/routes/ghostwriter.py:122` (entrypoint, get_context) — GET /api/ghostwriter/context/{job_id} takes a caller-chosen job_id and declares no identity or owner dependency. Only the global session gate in server/app.py _install_auth_gate applies.
- `server/routes/ghostwriter.py:28` (propagation, _get_job_and_profile) — JobsDB() is built with user_id=None, so get_by_id (db/jobs_db.py:699-703) adds no user_id filter and returns any user's job row.
- `server/routes/ghostwriter.py:32` (propagation, _get_job_and_profile) — ProfileDB().summary() returns the owner's global profile, including the _flat field map, whoever the caller is.
- `intelligence/ghostwriter.py:39` (propagation, build_context) — A second unscoped get_by_id adds the job's brief, tailored summary and skills. Lines 44-66 add the job description and the owner's name, role, location, education, LinkedIn, GitHub and portfolio.
- `server/routes/ghostwriter.py:129` (sink, get_context) — The full context is returned to the caller as JSON. The chat route (line 114, then intelligence/ghostwriter.py:190 LLM.chat) sends the same context as the system prompt on the global provider key. cover_letter (line 78) and interview_prep (line 93) do the same.

**Lower-trust principal.** A signed-in non-owner user (id 2) on an instance where sign-ups are open.

**Conditions.**

- *system_configuration*: ALLOW_SIGNUP=true, so accounts other than the owner (user id 1) can exist. The default is false (core/config.py:75).
- *authentication_level*: The attacker has a valid session for any non-owner account.
- *data_state*: To read another user's job, the attacker needs that job's id, which is sha256('<uid>:<normalized url>')[:16] and can be computed from a public posting URL. The owner's profile is exposed with any existing job id, including the attacker's own.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `GET /api/ghostwriter/context/3111c49c93334335 with user 2's session cookie (owner job id = sha256('1:https://jobs.example.test/posting/123')[:16])`
- Input: `GET /api/ghostwriter/context/<user 2's own job id> with user 2's session cookie`
- Input: `POST /api/ghostwriter/chat/3111c49c93334335 {"message":"hi","history":[]} with user 2's session cookie`
- Input: `POST /api/ghostwriter/cover-letter/<user 2's own job id> {} with user 2's session cookie`
- Input: `POST /api/ghostwriter/interview-prep/3111c49c93334335 with user 2's session cookie`
- Copy the source into sandbox scratch. In-process, point cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, UsersDB and SettingsDB at temp files. Set cfg.ALLOW_SIGNUP=True, cfg.COOKIE_SECURE=False and a 40+ character dummy APP_SECRET_KEY, pass a fresh APP_ENCRYPTION_KEY, then call create_app(ui_dir=<missing dir>) and use TestClient.
- Replace core.llm.chat and core.llm.chat_json with local recording stubs so no provider is contacted.
- Sign up owner@example.test (id 1) and user2@example.test (id 2). Set dummy owner profile fields full_name and linkedin_url. Add one dummy job for each user with JobsDB(user_id=...).add, putting a private marker in the owner's job description.
- As user 2, call /api/jobs/<owner job>, /api/kits/<owner job>/regenerate and the ghostwriter payloads. Also make an anonymous call and an owner call to the context route. Record status codes, context markers and stub calls. Verifier script: scratch work/repro.py. Result promoted from work/evidence/ghostwriter_cross_tenant_v06.json.

**Observed:** User 2 got 404 from GET /api/jobs/<owner job> and 403 from POST /api/kits/<owner job>/regenerate. GET /api/ghostwriter/context/<owner job> returned 200 with the owner's job title, the private job-description marker, the owner's full_name and the owner's linkedin_url. With user 2's own job id, the context still held the owner's name and LinkedIn. chat on the owner's job, cover-letter on user 2's own job, and interview-prep on the owner's job each returned 200. They made three LLM stub calls whose system prompts held the owner's profile, and the chat and interview-prep calls also held the owner's private job description. An anonymous request returned 401, and the owner's own request returned 200. The recomputed owner job id matched the stored id 3111c49c93334335.

**Smallest fix.** Make all four ghostwriter routes owner-only with Depends(require_owner), because they read the owner's global profile and spend the owner's global LLM key. Scope the job lookup to the caller with JobsDB(user_id=uid). Regression test: user 2 gets 403 or 404 from all four ghostwriter routes, and the owner still gets 200 for the owner's own job.

`server/routes/ghostwriter.py`:
```python
from fastapi import APIRouter, Depends, HTTPException
from core.auth import require_owner

def _get_job_and_profile(job_id: str, uid: int):
    db  = JobsDB(user_id=uid)
    job = db.get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    profile = ProfileDB().summary()
    return job, profile, db

@router.post("/api/ghostwriter/cover-letter/{job_id}")
async def cover_letter(job_id: str, data: dict = None, uid: int = Depends(require_owner)):
    job, profile, db = _get_job_and_profile(job_id, uid)
    ...
# same uid: int = Depends(require_owner) and _get_job_and_profile(job_id, uid) for interview_prep, chat and get_context
```

### 5. In --dev-ui mode, a client-supplied X-Forwarded-For passes through the loopback Next proxy and lets anyone bypass the login brute-force throttle

- **Fingerprint:** `core/throttle.py:LoginThrottle:dev-ui-proxy-forwards-client-xff-defeats-login-ratelimit`  
- **Severity:** medium (likelihood medium: Requires --dev-ui mode, which is documented as a development convenience. In that mode, `next dev` listens on all interfaces by default, and any non-browser client that can reach :3000 can set the header trivially with no authentication. — impact medium: Removes the only brute-force and password-spray control on login and password change, allowing unlimited online password guessing against the owner and other accounts. Account compromise still depends on the password being guessable.)  
- **Confidence:** high — Reproduced end to end in the sandbox with the real app, main.py's exact uvicorn.Config, and the shipped Next 16.3.7 proxyRequest/httpxy code. The no-XFF control throttled after 5 attempts, while 30 rotating-XFF attempts were never throttled and the correct password was then accepted. Only the Next routing code before proxyRequest was verified from source rather than executed: it has no middleware and does not mutate x-forwarded-for, and base-server's `??=` would keep a client value even if it ran.

**What it is.** In --dev-ui mode, main.py starts `next dev` (frontend/package.json runs `next dev` with no -H flag). frontend/next.config.mjs rewrites /api/:path* to the backend at http://127.0.0.1:8000. For an external rewrite, Next 16.3.7 calls router-utils/proxy-request.js proxyRequest(), which builds an httpxy ProxyServer. That ProxyServer sets only x-forwarded-host and does not enable xfwd. httpxy setupOutgoing copies every client request header ({...req.headers}), so a client's X-Forwarded-For reaches the backend unchanged. The backend runs as uvicorn.Config(app, host, port, log_level='warning') (main.py:187). uvicorn 0.54.0 defaults to proxy_headers=True and forwarded_allow_ips='127.0.0.1,::1', and the proxy connects from 127.0.0.1, so ProxyHeadersMiddleware replaces scope['client'] with the client's chosen value. server/routes/auth.py:_client_ip returns request.client.host. core/throttle.py:LoginThrottle uses that value in both of its keys: acct:{ip}|{email} with a limit of 5 per 15 minutes, and ip:{ip} with a limit of 20 per 15 minutes. If the attacker sends a new X-Forwarded-For on each attempt, every failure lands in a fresh bucket. Neither limit ever trips, on POST /api/auth/login or on POST /api/auth/change-password, which shares the same throttle (auth.py:120-129).

**Root cause.** The throttle counts failures per request.client.host. In --dev-ui mode that value comes from the client's own X-Forwarded-For. The Next dev rewrite proxy (httpxy, xfwd off) forwards that header unchanged from a loopback socket. uvicorn's default ProxyHeadersMiddleware, which main.py's uvicorn.Config leaves on, trusts X-Forwarded-For from loopback. Nothing between the client and the throttle removes or replaces the client's header. The comment in core/throttle.py assumes proxy headers are off unless --proxy-headers is passed, but uvicorn turns them on by default.

**Intended behaviour.** Failed logins should be counted against an identity the client cannot forge. A forwarded-for value should only be trusted when a trusted ingress sets it after stripping any copy the client sent. The local Next dev proxy does not strip it, so the backend should not trust it.

**Source path.**

- `frontend/next.config.mjs:31` (entrypoint, config.rewrites) — An unauthenticated client sends POST /api/auth/login to the Next dev server (:3000, started by `next dev`) with an X-Forwarded-For value of its choosing. The rewrite sends /api/:path* to http://127.0.0.1:8000 through Next's proxyRequest/httpxy. That path copies all client headers and overrides only x-forwarded-host.
- `main.py:187` (propagation, start_server) — uvicorn.Config(app, host=cfg.APP_HOST, port=cfg.APP_PORT, log_level='warning') leaves proxy_headers=True and forwarded_allow_ips='127.0.0.1,::1' at their defaults. The proxy connects from 127.0.0.1, so ProxyHeadersMiddleware rewrites scope['client'] from X-Forwarded-For.
- `server/routes/auth.py:58` (propagation, _client_ip) — Returns request.client.host, which is now the value the attacker chose. login() passes it to the throttle at line 64; change_password() does the same at line 120.
- `core/throttle.py:45` (sink, LoginThrottle._keys) — Both buckets, 'acct:{ip}|{email}' and 'ip:{ip}', are keyed only on that ip. A different X-Forwarded-For per attempt gives a different key each time, so retry_after never reaches per_account=5 or per_ip=20.

**Lower-trust principal.** An unauthenticated network client brute-forcing or password-spraying the owner's (or any user's) account while the instance runs in --dev-ui mode.

**Conditions.**

- *system_configuration*: The server is started with `python main.py --dev-ui`, so the Next dev server proxies /api to the loopback backend. Production mode, with the static export served directly by uvicorn and a non-loopback peer, is not affected by this path.
- *network_routing*: The attacker can send HTTP requests with a chosen X-Forwarded-For to the Next dev server on :3000. `next dev` is started without -H, so it listens on all interfaces by default and is reachable from the LAN or any network that can reach the host. A cross-site browser page cannot set this header in a simple request.
- *authentication_level*: No authentication is needed for POST /api/auth/login. POST /api/auth/change-password uses the same throttle but requires a session.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `POST /api/auth/login HTTP/1.1  Host: 127.0.0.1:3000  Content-Type: application/json  X-Forwarded-For: 203.0.113.1  {"email":"owner@example.test","password":"wrong0"}`
- Input: `POST /api/auth/login HTTP/1.1  Host: 127.0.0.1:3000  Content-Type: application/json  X-Forwarded-For: 203.0.113.2  {"email":"owner@example.test","password":"wrong1"}`
- Inside the OS-enforced sandbox (no external network, nobody uid, 1 GiB AS), from a writable copy of the source with a dummy APP_ENCRYPTION_KEY/APP_SECRET_KEY: build the real FastAPI app with server.app.create_app(), then serve it with exactly main.py's uvicorn.Config(app, host='127.0.0.1', port=8000, log_level='warning') wrapped by core.shutdown.make_server.
- Run a Node 22 front end on 127.0.0.1:3000 that calls the shipped Next 16.3.7 next/dist/server/lib/router-utils/proxy-request.js proxyRequest() for each request, with the destination from next.config.mjs (http://127.0.0.1:8000 + path). This is the same function router-server.js uses for external rewrites. A full `next dev` run was not used because it cannot initialize under the 1 GiB limit, so the Next routing layer before proxyRequest was checked from source instead: no middleware exists and nothing touches x-forwarded-for.
- Create the dummy owner owner@example.test with signup.
- Control: send 7 wrong-password logins through :3000 with no X-Forwarded-For.
- Attack: clear the in-memory throttle, then send 30 wrong-password logins through :3000, each with a different X-Forwarded-For (203.0.113.1-30). Follow with the correct password using X-Forwarded-For: 198.51.100.7.
- Result file: work/evidence/devproxy_xff_throttle.json (scratch, for parent promotion). The hunter's isolated middleware fixture is at agents/h12-devui/artifacts/work/evidence/xff_trust.json.

**Observed:** uvicorn 0.54.0, config.proxy_headers=True, forwarded_allow_ips='127.0.0.1,::1'. The control without XFF through the proxy returned [401,401,401,401,401,429,429], so the throttle works when the key is the real peer. With a rotating XFF through the same proxy, all 30 wrong guesses returned 401 with no 429, which is above both per_account=5 and per_ip=20. The correct password afterwards returned 200. The throttle table held keys like 'acct:203.0.113.10|owner@example.test' and no 'ip:127.0.0.1' entry, confirming the client-chosen XFF value was used as the key.

**Smallest fix.** Do not let a client-supplied X-Forwarded-For become the throttle identity. The narrowest source fix is to turn off uvicorn proxy-header trust in main.py's uvicorn.Config unless an operator opts in explicitly, and to require them to name the trusted proxy when they do. All --dev-ui traffic then counts against the real peer (127.0.0.1) instead of a value the attacker can rotate. Optionally, also stop the dev proxy from forwarding client X-Forwarded-For, and fix the misleading comment in core/throttle.py.

`main.py`:
```python
    config = uvicorn.Config(
        app, host=cfg.APP_HOST, port=cfg.APP_PORT, log_level="warning",
        # Trust X-Forwarded-* only when an operator explicitly fronts this with a
        # reverse proxy that strips client-supplied copies. Off by default so the
        # --dev-ui Next proxy (which relays client headers verbatim) cannot forge
        # request.client.host, which core/throttle.py keys the login limits on.
        proxy_headers=os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true",
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", ""),
    )
```

### 6. Non-owner accounts can read and overwrite the owner's global profile, resume.pdf and search preferences

- **Fingerprint:** `server/routes/onboarding.py:profile-resume-routes-missing-require-owner`  
- **Severity:** medium (likelihood low: It needs the non-default ALLOW_SIGNUP=true and a signed-in second account. Once those hold, a single authenticated request is enough. — impact high: It exposes the owner's profile PII. It also lets another user change the contact data, search targets and resume used in real applications submitted in the owner's name. Per source, a text-bearing upload also spends the instance's LLM key.)  
- **Confidence:** high — I re-read the full source lineage and reproduced the result myself in a sandboxed two-user TestClient run with dummy data. Owner-only control routes returned 403 as expected. Only the LLM-spend part rests on source alone.

**What it is.** These onboarding routes are protected only by the session gate: POST /api/setup/upload-resume, GET /api/profile, GET /api/profile/flat, POST /api/profile, GET/POST /api/search-prefs and GET /api/status. They act on the single machine-wide profile.db and resume.pdf. The code elsewhere treats both as belonging to account #1 (PROFILE_OWNER_ID / OWNER_USER_ID). When ALLOW_SIGNUP=true, any signed-in non-owner can read the owner's profile PII, including name, email and phone. A non-owner can also rewrite the owner's profile fields and search preferences, and replace resume.pdf. The orchestrator, the form fillers and the pipeline then use those values for the owner's real applications. Source shows that a text-bearing upload is then parsed by an LLM on the instance's configured provider key.

**Root cause.** server/routes/onboarding.py declares upload_resume (line 43), get_profile (72), get_profile_flat (77), update_profile (82), get_prefs (126), save_prefs (136) and get_status (174) without the require_owner dependency (core/auth.py:190). These handlers instantiate the unscoped, global ProfileDB() and write cfg.RESUME_PATH directly. No other layer applies the owner check: the auth gate in server/app.py (line 88) only resolves a valid session uid.

**Intended behaviour.** Only PROFILE_OWNER_ID may read or change the global profile, resume and search preferences. answer_bank._profile_for (server/routes/answer_bank.py:30) and Orchestrator._owner_profile (applier/session.py:624) refuse the profile to anyone else, and the require_owner comment (core/auth.py:176-188) says the same. A non-owner should get 403, or a per-user profile.

**Source path.**

- `server/routes/onboarding.py:81` (entrypoint, update_profile) — POST /api/profile accepts a dict from any signed-in user. The handler has no require_owner dependency.
- `server/app.py:88` (propagation, auth_gate) — The gate only resolves a valid session uid and stores it on request.state. It does no owner or authorization check.
- `server/routes/onboarding.py:97` (propagation, update_profile) — ProfileDB() opens the global cfg.PROFILE_DB_PATH, which is not scoped to the caller. Line 119 then writes the caller's values with set_many.
- `server/routes/onboarding.py:47` (propagation, upload_resume) — upload_resume overwrites the global cfg.RESUME_PATH with the caller's bytes. At line 54 it parses the file and re-imports it into the global profile.
- `applier/session.py:624` (sink, Orchestrator._owner_profile) — The owner's applications use profile_db.summary() from the global ProfileDB. Resume uploads fall back to cfg.RESUME_PATH (applier/form_filler.py:96). So values a non-owner wrote are used in the owner's applications.

**Lower-trust principal.** A signed-in non-owner (user id 2) on an instance with ALLOW_SIGNUP=true

**Conditions.**

- *system_configuration*: ALLOW_SIGNUP=true must be set so that a second account can exist. The default is false, and the second signup was refused with 403 while it was false.
- *authentication_level*: The attacker holds any valid signed-in session other than user id 1. Anonymous requests get 401.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `GET /api/profile/flat`
- Input: `POST /api/profile {"email":"attacker-v07@example.com"}`
- Input: `POST /api/search-prefs {"positions":["BOB-V07-ROLE"]}`
- Input: `POST /api/setup/upload-resume multipart file=x.pdf content '%PDF-1.4 BOB-V07-RESUME'`
- Copy the source into the scratch work/src and make it writable. Run work/repro/v07_repro.py in the OS sandbox with dummy APP_ENCRYPTION_KEY and APP_SECRET_KEY values. The script points the settings, jobs, users and profile DBs and RESUME_PATH at a temp dir, sets COOKIE_SECURE=False, stubs LLM.chat_json, and builds create_app() with a nonexistent ui_dir.
- Sign up owner@example.com (uid 1). Confirm that a second signup returns 403 while ALLOW_SIGNUP is false. Set cfg.ALLOW_SIGNUP=True and sign up bob@example.com (uid 2).
- As the owner, POST /api/profile with a dummy email and phone, and write a dummy resume.pdf.
- As bob, send each payload. Then, as the owner, read /api/profile/flat and /api/search-prefs, and read the bytes of resume.pdf. As a control, check that bob gets 403 on the owner-only /api/notifications/settings and that an anonymous client gets 401.
- Results file for promotion: work/evidence/v07_owner_profile_nonowner.json.

**Observed:** Bob's GET /api/profile/flat returned 200 with email owner-v07@example.com and phone +10000000777. GET /api/profile and /api/status also returned 200. Bob's POST /api/profile returned 200 (updated 1), and the owner's email then read attacker-v07@example.com. Bob's POST /api/search-prefs returned 200, and the owner's positions became ['BOB-V07-ROLE']. Bob's upload returned 200, and resume.pdf then contained '%PDF-1.4 BOB-V07-RESUME'. Controls: bob got 403 on /api/notifications/settings, and anonymous got 401 on /api/profile/flat. The dummy PDF had no extractable text, so the LLM-spend path was not exercised and rests on source only.

**Smallest fix.** Add the require_owner dependency to every onboarding route that touches the global profile, resume or search preferences. Long term, scope ProfileDB and the resume by user_id, as MULTI_USER_STATUS.md plans. Add a regression test that a second user gets 403 on each route and that the owner's profile and resume.pdf stay unchanged.

`server/routes/onboarding.py`:
```python
from core.auth import get_current_user_id, require_owner

@router.post("/api/setup/upload-resume")
async def upload_resume(file: UploadFile = File(...), _owner: int = Depends(require_owner)): ...

@router.get("/api/profile")
async def get_profile(_owner: int = Depends(require_owner)): ...

@router.get("/api/profile/flat")
async def get_profile_flat(_owner: int = Depends(require_owner)): ...

@router.post("/api/profile")
async def update_profile(data: dict, _owner: int = Depends(require_owner)): ...

@router.get("/api/search-prefs")
async def get_prefs(_owner: int = Depends(require_owner)): ...

@router.post("/api/search-prefs")
async def save_prefs(data: dict, _owner: int = Depends(require_owner)): ...

@router.get("/api/status")
async def get_status(_owner: int = Depends(require_owner)): ...
```

### 7. /api/setup/upload-resume accepts uploads of any size and runs PDF parsing and the LLM call on the server's single event loop, stalling every other request

- **Fingerprint:** `server/routes/onboarding.py:upload_resume:unbounded-upload-sync-parse-on-event-loop`  
- **Severity:** low (likelihood low: Needs a signed-in account. Cross-user impact needs ALLOW_SIGNUP=true, which is off by default. Once those hold, one small request is enough, and it can be repeated at will. — impact medium: While each upload's extraction and LLM call run, every request on the single process's event loop waits, from every user, including the owner. Repeated uploads keep the service stalled. The requester also chooses the size of the persistent file written. The effect is temporary and recovers when the request finishes.)  
- **Confidence:** high — The source shows synchronous blocking calls inside an async handler with no bounds. A local run against a real uvicorn server, with no timing stub, showed real pdfplumber work on the loop thread delaying a concurrent request from another user by about the extraction time. The LLM-latency contribution comes from the source and was not measured, because no provider is available.

**What it is.** POST /api/setup/upload-resume is an `async def` handler protected only by the session gate. The one input check is that the filename ends in `.pdf`. It copies the whole upload, with no size limit, into the single machine-wide cfg.RESUME_PATH. It then calls core.resume_parser.parse() synchronously, inside the handler. That function extracts text from every page with pdfplumber, with no page or byte cap, and then makes a blocking LLM.chat_json provider call, which can be two calls on invalid JSON. All of this runs on the one uvicorn event-loop thread (main.py starts a single uvicorn.Server). While it runs, every other request on that loop waits, from every user: HTTP, SSE and WebSocket. The sibling route /api/resumes enforces the 5 MB cap and moves its work off the loop with asyncio.to_thread. Independent local check against a real uvicorn server on loopback, with no timing stubs: (1) a non-owner signed-in user (id 2) uploaded 12,582,921 bytes named x.pdf; the route returned 200 and wrote the full 12,582,921 bytes to RESUME_PATH; (2) the same user uploaded a valid 60-page, 50 KB PDF, which is well under the sibling's 5 MB cap; the actual pdfplumber extraction took 3.63 s on the event-loop thread, and the owner's concurrent GET /api/auth/me took 3.8 s, against 0.0 s when idle. A 300-page variant used up the sandbox's 1 GiB address-space limit, so the stall grows with page count. A size cap alone would therefore not prevent the stall.

**Root cause.** upload_resume (server/routes/onboarding.py:42-66) is declared async but does three kinds of blocking work directly on the event loop: synchronous file I/O (shutil.copyfileobj, line 48), pdfplumber extraction over all pages (core/resume_parser.py:79-83, called from parse at line 51), and a synchronous LLM.chat_json network call (core/resume_parser.py:60; core/llm.py:92 makes up to two chat() calls). It also has no size cap, no PDF signature check and no page cap. The equivalent controls exist in db/resumes_db.py (check_pdf at line 89; TEXT_PAGES=4 in extract_text at line 143) but are not used here.

**Intended behaviour.** Resume upload should apply the same bounds as resume variants: at most 5 MB, a %PDF- signature and a page cap on extraction. It should write the file atomically. PDF extraction and the LLM call should run off the event loop (core.llm.run_blocking or asyncio.to_thread, with a timeout), so one upload cannot stall other users' requests or write a file of whatever size the requester chooses.

**Source path.**

- `server/routes/onboarding.py:42` (entrypoint, upload_resume) — POST /api/setup/upload-resume, reachable by any valid session (auth gate only, no require_owner)
- `server/routes/onboarding.py:44` (propagation, upload_resume) — Only file.filename.lower().endswith('.pdf') is checked: no size limit, no signature check
- `server/routes/onboarding.py:48` (propagation, upload_resume) — shutil.copyfileobj writes the whole upload into cfg.RESUME_PATH, truncating it in place, with blocking I/O on the loop
- `server/routes/onboarding.py:54` (propagation, upload_resume) — parse(cfg.RESUME_PATH) is called synchronously inside the async handler, on the event-loop thread
- `core/resume_parser.py:79` (propagation, _extract_text) — pdfplumber opens the file and extracts text from every page (lines 80-83), with no page or byte cap; the text is truncated to 12000 characters only after extraction (line 61)
- `core/resume_parser.py:60` (sink, parse) — LLM.chat_json makes a blocking provider call, retried once on invalid JSON (core/llm.py:92), still on the event loop

**Lower-trust principal.** A signed-in non-owner user on an instance with ALLOW_SIGNUP=true

**Conditions.**

- *authentication_level*: Any valid session cookie. The route has no require_owner; the missing owner check is reported separately by the peer access-control unit.
- *system_configuration*: Cross-user impact needs a non-owner account, which requires ALLOW_SIGNUP=true (server/routes/auth.py:44, off by default). Otherwise only the owner can trigger it, and the stall affects only the owner.
- *data_state*: The stall length grows with the PDF's page count and content (3.6 s for 60 simple pages locally) and, in a configured deployment, with LLM provider latency. The size of the file on disk is whatever the requester sends.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `multipart/form-data field 'file', filename 'x.pdf', body b'%PDF-1.4\n' + b'0' * 12582912`
- Input: `multipart/form-data field 'file', filename 'big.pdf', body a valid 60-page reportlab PDF (50,470 bytes, 50 text lines per page)`
- Copied the source into the v08 scratch and ran it inside sbx.sh (no external network, isolated loopback, uid nobody, 1 GiB address space). Used a fresh APP_ENCRYPTION_KEY, a dummy APP_SECRET_KEY of 40+ characters and ALLOW_SIGNUP=true. All databases and RESUME_PATH were redirected to a temp dir. No LLM is reachable, so the call fails fast and falls back to regex parsing.
- Started the real app with uvicorn.Server on 127.0.0.1:18765 in-process (one event loop). Signed up the owner (id 1) and a member (id 2, owner=false) over HTTP with separate cookie jars.
- Wrapped core.resume_parser._extract_text with a probe that only records whether an asyncio loop is running in its thread and how long the original takes. The probe adds no delay.
- The member POSTed the 12 MiB payload to /api/setup/upload-resume, and the size of RESUME_PATH was recorded. The member then POSTed the 60-page PDF; 0.5 s later the owner sent GET /api/auth/me and its latency was measured.
- Harness: scratch/work/harness/v08_upload.py. Output: work/evidence/v08_upload_resume.json, to be promoted to agents/v08/artifacts/work/evidence/v08_upload_resume.json.

**Observed:** 12 MiB upload: 200 {"status":"ok"}, RESUME_PATH = 12,582,921 bytes, equal to the bytes sent. 60-page PDF upload: 200 in 4.3 s; probe on_event_loop_thread=true, extract_seconds=3.63. The owner's concurrent /api/auth/me returned 200 after 3.8 s; idle latency before and after was 0.0 s. A 300-page variant hit MemoryError under the sandbox's 1 GiB address-space limit, and the run was not continued.

**Smallest fix.** Bound the upload: read at most MAX_BYTES+1 and call db.resumes_db.check_pdf for the size and signature check. Write to a temp file and os.replace it into place. Run parsing and the LLM call off the loop with core.llm.run_blocking (thread pool plus timeout), and cap extracted pages, as TEXT_PAGES does. Both changes are needed, because a valid sub-5 MB PDF still stalls the loop. Regression tests: a 6 MB upload returns 413, and a concurrent request is not delayed while a slow parse runs.

`server/routes/onboarding.py`:
```python
@router.post("/api/setup/upload-resume")
async def upload_resume(file: UploadFile = File(...), uid: int = Depends(require_owner)):
    from db.resumes_db import MAX_BYTES, ResumeError, check_pdf
    data = await file.read(MAX_BYTES + 1)
    try:
        check_pdf(data)
    except ResumeError as e:
        raise HTTPException(e.status, str(e))
    tmp = cfg.RESUME_PATH.with_suffix('.pdf.part')
    tmp.write_bytes(data)
    os.replace(tmp, cfg.RESUME_PATH)
    from core.llm import run_blocking
    from core.resume_parser import parse
    parsed = await run_blocking(parse, cfg.RESUME_PATH)
    await run_blocking(ProfileDB().import_from_parsed, parsed)
    ...  # plus: cap pages in core/resume_parser._extract_text (pdf.pages[:N])
```

### 8. An open SSE event stream (and WebSocket) keeps sending a user's job events after their sessions are revoked

- **Fingerprint:** `server/sse.py:stream:no-session-revalidation`  
- **Severity:** low (likelihood low: The attacker needs the victim's valid session cookie and must already have a connection open when the victim revokes. Revocation exists for exactly the stolen-session case, so the precondition is realistic but narrow. — impact low: Read-only access to the revoked user's own new job timeline events (job ids, stages, step messages, screenshot names), or to apply progress for one job, continues for the life of one connection. There is no write access, no other user's data, and no wider privilege.)  
- **Confidence:** high — Reproduced independently in the sandbox: after logout-everywhere the old cookie got 401 on new requests, while the stream that was already open delivered an event created after the revocation. The source shows no re-check in either loop. The WebSocket variant is from source only.

**What it is.** GET /api/events/stream checks the session cookie once, when the request arrives, through the get_current_user_id dependency (core.auth.user_for_token). After that, server/sse.py stream() polls the user's application_events rows in an endless loop, sending keep-alive comments, and never checks the session again. Its only exit is the client disconnecting. Suppose someone holds a copy of a user's session cookie and has a stream open. The user then uses 'sign out everywhere', changes their password, or is deactivated. Every new request with the old cookie now gets 401, including a fresh stream. But the stream that was already open keeps sending each new event for the user's jobs: job id, stage changes, step code and message, actor and screenshot name. This lasts until the holder disconnects. By source, /ws/{job_id} in server/websocket.py has the same connect-time-only check: it calls user_for_token once and then loops forever sending queued apply-progress events and pings. Either way, the revocation promised in the server/app.py auth-gate comment ('ends sessions now, not whenever the 14-day token happens to expire') does not reach connections that are already open.

**Root cause.** Authorization for long-lived responses is checked only when the connection opens. sse.stream() (server/sse.py:61-74) loops on db.events_since with no re-check. The route (server/routes/applications.py:90) passes it only is_disconnected, not the token or any re-check callback. The ws_endpoint loop (server/websocket.py:79-84) never calls core.auth.user_for_token again either. A bumped session_version or is_active=0, which user_for_token checks at core/auth.py:137 and db/users_db.py:146, therefore has no effect on connections already open.

**Intended behaviour.** Once a user's session version changes or the account is deactivated, every connection authenticated with an old token should stop sending data within a short, bounded time, the same way new HTTP requests and new WebSocket connects already get rejected.

**Source path.**

- `server/routes/applications.py:86` (entrypoint, event_stream) — GET /api/events/stream. The get_current_user_id dependency checks the cookie through user_for_token once, at request time, and gives back uid.
- `server/routes/applications.py:90` (propagation, event_stream) — sse.stream(JobsDB(user_id=uid), last-event-id, is_disconnected=request.is_disconnected) is wrapped in a StreamingResponse. Neither the session token nor a re-check callback is passed in.
- `server/routes/auth.py:142` (propagation, logout_everywhere) — bump_session_version increments users.session_version. From then on user_for_token rejects the old token (core/auth.py:137), but only when something calls it again.
- `server/sse.py:67` (sink, stream) — Inside the while-True loop (line 61), every new application_event from db.events_since is sent as format_event(ev) to the connection that was already authenticated, with no session re-check.

**Lower-trust principal.** Someone holding a copy of the victim's session cookie who opened GET /api/events/stream before the victim used 'sign out everywhere'.

**Conditions.**

- *authentication_level*: The attacker must already hold a valid session cookie for the victim, for example a stolen or leaked one, and must open the stream or WebSocket before the victim revokes it.
- *timing_dependency*: Data keeps flowing only while that one connection stays open. Once it drops, reconnecting with the revoked cookie gets 401 (SSE) or close 1008 (WebSocket), so Last-Event-ID resume does not help.
- *data_state*: Only the victim's own events created after the revocation are exposed: their jobs' timeline on the SSE stream, or apply progress for the one job on the WebSocket. Other users' data cannot be reached this way.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `GET /api/events/stream with Cookie: apex_session=<victim token issued before revocation>`
- Input: `POST /api/auth/logout-everywhere with the victim's cookie`
- Input: `JobsDB(user_id=1).record_step(job_id, 'opened_page', 'v11 post-revocation dummy step')`
- Copy the source into scratch and make it writable. Inside the OS sandbox (loopback only, uid nobody), start create_app(ui_dir=None) under uvicorn on 127.0.0.1, with a fresh Fernet APP_ENCRYPTION_KEY, a dummy APP_SECRET_KEY of 40+ characters, COOKIE_SECURE=false and ALLOW_SIGNUP=true. Script: scratch/work/scripts/repro.py.
- POST /api/auth/signup for dummy user 1 (victim@example.test) and keep the apex_session cookie as the copied cookie. Add a dummy job with JobsDB(user_id=1).add(...).
- With the copied cookie, open a streaming GET /api/events/stream in a background thread and wait about 1.5 seconds.
- POST /api/auth/logout-everywhere with the same cookie. Confirm that the old cookie now gets 401 on GET /api/stats and on a new GET /api/events/stream.
- Record a new step event for the victim's job with JobsDB(user_id=1).record_step(...), wait 3 seconds, and read the data: frames the already-open stream received. The results are in work/evidence/v11_sse_after_revocation.json, promoted to agents/v11/artifacts/work/evidence/v11_sse_after_revocation.json.

**Observed:** logout-everywhere returned 200 {"status":"ok"}. With the old cookie, /api/stats and a new /api/events/stream both returned 401 {"detail":"Not logged in"}. The stream opened before the revocation (HTTP 200) then received 'data: {"id": 2, "job_id": "42ebafbf04d94546", ..., "kind": "step", "from_stage": "found", "to_stage": "found", "actor": "agent", "detail": {"step": "opened_page", "message": "v11 post-revocation dummy step"}, "screenshot": ""}', an event created after the revocation. This matches the hunter's independent result.

**Smallest fix.** Re-check the session inside long-lived handlers. The SSE route should capture the original request's session token and pass sse.stream a callback that re-runs core.auth.user_for_token on it (off the event loop) and compares the user id. The stream should call it at most every few seconds and always before yielding a batch, and end the generator when the check fails. In the WebSocket loop, re-run user_for_token on each 25-second timeout and before sending each queued message, require the same user id, and close with code 1008 on failure. Add regression tests that logout-everywhere, change-password and deactivation each end an open SSE stream and an open WebSocket.

`server/routes/applications.py`:
```python
import asyncio
...
@router.get("/api/events/stream")
async def event_stream(request: Request, uid: int = Depends(get_current_user_id)):
    from core.auth import SESSION_COOKIE, user_for_token
    token = request.cookies.get(SESSION_COOKIE)
    async def still_authorized() -> bool:
        user = await asyncio.to_thread(user_for_token, token)
        return user is not None and user["id"] == uid
    gen = sse.stream(JobsDB(user_id=uid), request.headers.get("last-event-id"),
                     is_disconnected=request.is_disconnected, still_authorized=still_authorized)
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```
`server/sse.py`:
```python
async def stream(db, last_event_id=None, *, poll=POLL_SECONDS, heartbeat=HEARTBEAT_SECONDS,
                 is_disconnected=None, still_authorized=None, recheck=5.0):
    ...
    checked = time.monotonic()
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        if still_authorized is not None and time.monotonic() - checked >= recheck:
            if not await still_authorized():
                return
            checked = time.monotonic()
        events = await asyncio.to_thread(db.events_since, last)
        ...
```
`server/websocket.py`:
```python
    token, uid = websocket.cookies.get(SESSION_COOKIE), user["id"]
    async def still_ok() -> bool:
        u = await asyncio.to_thread(user_for_token, token)
        return u is not None and u["id"] == uid
    ...
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                msg = {"event": "ping", "data": {}}
            if not await still_ok():
                await websocket.close(code=1008)
                return
            await websocket.send_json(msg)
```

### 9. Approved submit re-reads the live form and submits field values that were never in the reviewed or auto-approved packet

- **Fingerprint:** `applier/universal.py:run_submit:fresh-reanalysis-not-bound-to-review-packet`  
- **Severity:** low (likelihood low: The form has to change between the review visit and the submit visit, or the model has to label fields differently on the second analysis. Then a human approval or a matching autonomy rule is needed, with dry_run off. — impact low: A real application goes out in the owner's name with profile facts and AI or placeholder answers the owner never reviewed, and autonomy's all-answers-known policy is bypassed. The recipient is the party the owner chose to apply to. A page the operator scripts already sees the review-pass values. No other principal, tenant or credential is affected.)  
- **Confidence:** medium — The missing binding is clear in the source. It was reproduced independently through the real run_submit, AnswerContext, form_filler.execute/click_submit and the review/autonomy code. The adapter variant needs no model. A live page changing between visits, and the model's exact relabeling, were not observed.

**What it is.** A person can approve a review packet (POST /api/applications/{id}/review/approve, or /api/apply/submit), or an autonomy rule can approve it at session.py:749. Orchestrator.submit then copies the packet's values into application_answers, keyed by normalized label (session.py:469). The submit pass does not submit the packet. universal.run_submit reopens the job URL and runs a fresh analyze() on the form the site serves now. AnswerContext.apply replaces values only for labels that match the packet. execute() then fills every field that has a fill_value: the LLM db_value or placeholder, or the adapter's profile value in adapters/runner.run_submit. click_submit / adapter.submit then sends the form. Nothing compares the submitted field set or values with the approved packet. This breaks the contract stated at review.py:30-31 and in the approve route docstring (server/routes/review.py:122-123). It has two triggers. (1) The form changes between the review visit and the submit visit, for example an employer on a hosted ATS or job board adds a question. (2) The model labels a field differently on the second analysis, so the reviewed value or the owner's own edit does not match and a fresh guess is submitted. Either way, profile facts (phone, LinkedIn, date of birth through the LLM) and AI-guessed or placeholder answers go out in the owner's application without review. The require_all_answers_known autonomy gate is bypassed the same way, because answers_known() ran only against the earlier packet. Limit: a page the operator scripts already receives every value during the review pass, because run_application fills the live page at universal.py:189 before approval. The new effect is unreviewed content in the submitted application, sent to the party the owner applied to. It is not a disclosure to some other principal.

**Root cause.** Approval binds only to a label->value map (review.reviewed_answers), which the submit pass uses as a preferred answer source. Neither universal.run_submit nor adapters/runner.run_submit checks, before click_submit or adapter.submit, that every non-file field it fills is in the approved packet with the same value. So a fresh read of a possibly changed page decides part of the submitted action.

**Intended behaviour.** review.py:30-31, the approve route docstring and Orchestrator._use_reviewed_values all say approval submits the reviewed values. If the submit pass would fill a non-file field whose normalized label is missing from the packet, or with a different value, the submit should stop and the application should go back to review. Autonomy approval should be held to the same rule, because its require_all_answers_known check covered only the packet.

**Source path.**

- `server/routes/review.py:130` (entrypoint, approve) — The owner's review approval (require_owner) calls apply_routes.submit. The autonomy path enters at applier/session.py:749, self.submit(None, emit, actor="autonomy").
- `server/routes/apply.py:190` (propagation, submit.run) — Orchestrator(job).submit(corrections or None, emit) runs in a background thread.
- `applier/session.py:469` (propagation, Orchestrator._use_reviewed_values) — Packet values are merged into session.application_answers by normalized label. This is the only link between the submit pass and the approved packet.
- `applier/session.py:358` (propagation, Orchestrator.submit) — The submit pass is dispatched through _execute with dry_run read from settings (false by default).
- `applier/universal.py:366` (propagation, run_submit) — A fresh analyze() of the page as served now. Its output decides the field list, selectors, values and submit_selector.
- `applier/universal.py:374` (propagation, run_submit) — _apply_answers -> AnswerContext.apply replaces db_value only for labels that match the reviewed answers. Fields that do not match keep the fresh LLM value or placeholder.
- `applier/universal.py:376` (propagation, run_submit) — execute() fills every field with a non-empty fill_value, reviewed or not.
- `applier/universal.py:395` (sink, run_submit) — click_submit sends the application in the owner's name, with no comparison against the approved packet.

**Lower-trust principal.** An employer or form operator whose application form changes between the agent's review visit and its submit visit. The same gap also opens with no adversary, when the model's second analysis labels a field differently.

**Conditions.**

- *third_party_dependency*: The application form serves extra or changed fields on the submit visit, for example an employer edits the questions on a hosted ATS or job-board form. Or the fresh analysis labels a reviewed field differently, so the packet value does not match.
- *user_interaction*: The owner approves the review packet, or an enabled autonomy rule matches. The dry_run setting is off.
- *authorization_role*: The submit runs with the owner's authority and browser session. The party that changes the form needs no account on the instance.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `Approved packet: [{label: 'Email', value: 'owner@example.test', source: 'profile', confidence: 0.95}]`
- Input: `Universal submit-pass analyze() stub: Email (db_value owner@example.test), 'Date of birth' (db_value '1990-01-01'), textarea 'Share my profile with partner recruiters' (placeholder 'yes'), submit_selector '#submit'`
- Input: `Adapter submit-pass extract_fields(): Email, Phone, LinkedIn profile (standard keys answered from the dummy profile, no LLM)`
- Copy the source into the scratch directory and run /mnt/work/harness/h.py with python3 -I in the sandbox, with dummy APP_ENCRYPTION_KEY and APP_SECRET_KEY. There is no network, browser or LLM.
- Build the packet with review.build_fields from one Email snapshot whose value equals the answered profile value. Record autonomy.answers_known(packet) and rule_failures for a '*' rule with require_all_answers_known=True.
- Load review.reviewed_answers(packet) into a MemoryState.application_answers and wrap it in an AnswerContext over a bank that resolves nothing, as Orchestrator._use_reviewed_values does.
- Variant A: stub universal.analyze and extract_fields. Keep the real form_filler.execute and click_submit, driven by a fake page that records fills and clicks. Call universal.run_submit(page, job, profile, None, dry_run=False, answers=ctx).
- Variant B: call adapters.runner.run_submit with a fake Adapter whose extract_fields returns Email, Phone and LinkedIn profile, and record adapter.fill and adapter.submit.
- Output is written to work/evidence/v03_submit_binding.json for parent promotion.

**Observed:** answers_known(packet)=true and rule_failures=[]. Variant A: the real execute() typed '1990-01-01' into #dob and 'yes' into #share. Neither field is in the packet. The real click_submit clicked '#submit', and run_submit returned {success: true, dry_run: false}. Variant B: with no LLM, the adapter path filled Phone '+10000000000' and LinkedIn profile from the profile, neither of which is in the packet. It called adapter.submit(dry_run=False) and returned success=true.

**Smallest fix.** Pass the approved packet's reviewed answers into the submit pass, in both universal.run_submit and adapters/runner.run_submit. Before click_submit or adapter.submit, refuse to submit if any non-file field with a non-empty fill value has a normalized label missing from the packet, or a value different from the packet's. Return a failure that sends the job back to review. Skip the check only when no packet exists (the legacy flow), or when the owner gave free-text corrections, which are meant to re-derive values. Regression test: the harness above must return success=false without clicking submit, and an unchanged form must still submit.

`applier/universal.py`:
```python
from db.answer_bank_db import normalize_question

def _unreviewed(fields, reviewed: dict[str, str]) -> list[str]:
    out = []
    for f in fields:
        if (f.field_type or '').lower() in ('file', 'upload'):
            continue
        v = (f.fill_value() or '').strip()
        if v and (reviewed.get(normalize_question(f.label)) or '').strip() != v:
            out.append(f.label)
    return out

# run_submit(..., reviewed: dict[str, str] | None = None): after _apply_answers(...), before execute():
        if reviewed is not None:
            extra = _unreviewed(analysis.fields, reviewed)
            if extra:
                return _fail('The form changed since review - not submitted: ' + '; '.join(extra[:3]))
```
`applier/adapters/runner.py`:
```python
# run_submit(..., reviewed: dict[str, str] | None = None): after _read(...), before _fill_and_report(...):
    if reviewed is not None:
        extra = U._unreviewed(analysis.fields, reviewed)
        if extra:
            return U._fail('The form changed since review - not submitted: ' + '; '.join(extra[:3]))
```

### 10. Autonomy auto-submits past the daily apply limit when the runner executor is used

- **Fingerprint:** `applier/autonomy.py:consider_auto_approval:daily-cap-ignores-in-flight-runner-submits`  
- **Severity:** low (likelihood low: The owner must have turned on autonomy with a rule whose max_per_day is above the daily cap, chosen the runner executor, turned dry_run off, and have more eligible queued jobs than the remaining cap. With those settings it happens in normal operation, with no attacker involved. — impact medium: Irreversible real job applications are sent in the owner's name beyond the configured safety cap (5 against 2 in the reproduction; in general the whole eligible queue, bounded only by the rule's max_per_day). This weakens the main limit on unattended LLM-driven submissions and the anti-ban pacing on job sites. Only the owner's own account is affected.)  
- **Confidence:** high — Every trace line was re-read in source. An independent sandbox harness using the real QueueWorker, Orchestrator, autonomy and RunnerHub code reproduced 5 submissions against a cap of 2, and a control run showed the cap holding when no submits were in flight. That isolates the uncounted in-flight runner submits as the cause.

**What it is.** With executor=runner (or executor=auto while one of the owner's runner tokens was seen recently), the queue worker turns every queued job into a runner apply task in back-to-back steps. A first pass left for a runner returns before the pacing delay (_not_before) is set, and the worker's own daily-cap check (_blocker) counts only jobs already at 'submitted'. The runner hub gives out tasks oldest first, so every apply task finishes before any submit task created later. As each apply result reaches ready_for_review, Orchestrator._after_review calls autonomy.consider_auto_approval. Its cap check compares JobsDB.count_applied_today() with queue_worker.daily_limit(settings). That count includes only jobs whose applied_at was set by the move to 'submitted', so it reads 0 at every check. Every matching job is auto-approved and gets a non-dry-run submit task, and when the hub hands that task out it checks only the job's stage. An independent sandbox run reproduced it: 5 real (non-dry-run) submissions against an effective cap of 2, with 0 autonomy_blocked steps. In a control run where the runner finished each job's apply and submit before the next worker step, the cap held at 2 and the worker returned 'limit'.

**Root cause.** The daily-cap checks in applier/autonomy.py consider_auto_approval (line 220) and QueueWorker._blocker (applier/queue_worker.py:281) both rely on JobsDB.count_applied_today(). That counts only jobs whose applied_at is today, and applied_at is written only on the transition to 'submitted' (db/jobs_db.py:493). Auto-approvals already granted whose real submit is deferred to a runner task are not counted. Runner-deferred first passes skip the pacing delay (applier/queue_worker.py:378), and RunnerHub._claim_valid (applier/runner_hub.py:322) does not re-check the cap before handing out a submit task. So nothing limits in-flight approved submissions.

**Intended behaviour.** Autonomy must not approve or send more real (non-dry-run) submissions per day than queue_worker.daily_limit(settings). The applier/autonomy.py docstring makes 'the daily apply limit has room' a precondition for auto-approval. preview_rule (applier/autonomy.py:270) counts the jobs it would approve ahead of a job ('applied_today + taken >= cap'), which shows that approved-but-not-yet-sent submissions are meant to count toward the cap.

**Source path.**

- `applier/queue_worker.py:315` (entrypoint, QueueWorker.step) — The owner-started background queue worker checks the daily cap through _blocker. _blocker counts only submitted jobs (0 while every pass is still with the runner), so the worker claims the next queued job.
- `applier/queue_worker.py:378` (propagation, QueueWorker._run_job) — A first pass left for a runner returns before _not_before is set. The worker claims the next job straight away, so every queued job becomes a runner apply task in consecutive steps.
- `applier/executors.py:109` (propagation, RunnerExecutor.execute) — The pass is queued as a runner task and the Orchestrator returns deferred.
- `db/runner_db.py:303` (propagation, RunnerTaskStore.claim) — Tasks are handed out oldest first (ORDER BY created_at, rowid), so all apply tasks are claimed before any submit task created later.
- `applier/runner_hub.py:471` (propagation, RunnerHub.result (apply task)) — Each apply result continues through Orchestrator.finish_pass -> _finish_first_pass -> ready_for_review -> _after_review.
- `applier/autonomy.py:220` (propagation, consider_auto_approval) — cap, done = daily_limit(settings), jobs.count_applied_today(). 'done' leaves out approvals whose submit task is still queued, so every job passes the cap check.
- `applier/session.py:749` (propagation, Orchestrator._after_review) — The matched rule triggers self.submit(actor='autonomy'). This queues a runner submit task with dry_run False and does no further limit check.
- `applier/runner_hub.py:471` (sink, RunnerHub.result (submit task)) — Each submit result goes through Orchestrator._finish_submit and moves the job to 'submitted' with applied_at set, past the configured cap. The real application has been sent in the owner's name.

**Lower-trust principal.** No external attacker. The unattended autonomy agent, acting for the owner, exceeds the owner's daily safety cap on irreversible real-world actions (job applications sent in the owner's name). This weakens the LLM-to-action guard that the architecture lists (dry_run, the daily limit, review approval, autonomy rules).

**Conditions.**

- *authorization_role*: The owner has turned autonomy on (autonomy_enabled) and has at least one enabled rule whose ledger conditions are met and whose max_per_day is above the remaining daily cap.
- *system_configuration*: executor=runner, or executor=auto while a runner token was seen recently; dry_run is off; the site type is in auto (not assist) apply mode.
- *data_state*: More queued jobs that pass the quality gate than the daily cap has room for.
- *timing_dependency*: The worker queues the next apply task right after the previous one is deferred, much faster than a runner can fill a form. So in normal operation all apply tasks exist before any submit task and the FIFO runner finishes them first. If the runner keeps up and finishes apply and submit before the next worker step, the cap holds (control run).

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `Settings: executor=runner, quality_first=true, daily_apply_limit=2, auto_skip_score_threshold=30, dry_run=false, min_delay_between_applies=0, max_delay_between_applies=0`
- Input: `AutonomyStore(1).set_enabled(True); add_rule({site_type:'*', min_score:0, max_per_day:100, min_attempts:1, min_success_rate:0.5, require_all_answers_known:false}); one approved_unchanged greenhouse attempt seeded in the ledger`
- Input: `5 dummy jobs https://boards.greenhouse.io/acme/jobs/90..94 with ai_score 80, queued via queue_worker.queue_jobs`
- Input: `Simulated runner: RunnerHub.claim then RunnerHub.result({success:true, review:{fields:[], url:'https://x.invalid/apply'}}) until no task is left`
- Copy the source into the sandbox scratch (work/src, made writable) and run /mnt/work/v01_cap.py inside sbx.sh with APP_ENCRYPTION_KEY (a fresh Fernet key) and a dummy APP_SECRET_KEY of 40+ characters. Run each scenario in its own process: 'python3 /mnt/work/v01_cap.py Cap.test_a_queue_drained_first' and 'python3 /mnt/work/v01_cap.py Cap.test_b_control_interleaved'. The harness subclasses tests/test_runner.HubBase (temp DBs, urlopen and Playwright patched to fail) and patches applier.kit.prepare_kit to a no-op, so no LLM or network is used.
- Scenario A: call QueueWorker(1).step() 6 times, then let the simulated runner claim and report every task in FIFO order. Scenario B (control): after each worker step, let the runner drain all tasks before the next step.
- Compare the number of jobs at stage 'submitted', and the autonomy_approved / autonomy_blocked step counts, with queue_worker.daily_limit(settings).
- Evidence (promotion allowlist): work/evidence/v01_autonomy_runner_cap_a.json and work/evidence/v01_autonomy_runner_cap_b.json.

**Observed:** Scenario A: effective_daily_limit=2; worker outcomes processed x5 then idle; runner log shows apply x5 (applied_today 0 before and after each), then submit x5 (applied_today 0->1->2->3->4->5); all 5 jobs reached 'submitted'; autonomy_approved_steps=5, autonomy_blocked_steps=0, count_applied_today=5. Scenario B control: worker outcomes processed, processed, limit x4; 2 submitted, 3 still queued, count_applied_today=2. The cap holds only when no submits are in flight.

**Smallest fix.** Count approved-but-not-yet-sent real submissions toward the cap. In consider_auto_approval, add today's open non-dry-run submit tasks (or auto_approved attempts not yet submitted) to count_applied_today() before comparing with the cap. Apply the same accounting in QueueWorker._blocker. Re-check the cap in RunnerHub._claim_valid before handing out a non-dry-run submit task, and hold or cancel it if the cap is reached. Optionally apply the pacing delay to runner-deferred passes too.

`applier/autonomy.py`:
```python
        if not dry_run:
            from db.runner_db import RunnerTaskStore
            in_flight = RunnerTaskStore(owner_of(job), path).count_open(kind="submit")
            cap, done = daily_limit(settings), jobs.count_applied_today() + in_flight
            if cap > 0 and done >= cap:
                ...  # record autonomy_blocked and return None as today
```
`db/runner_db.py`:
```python
    def count_open(self, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) n FROM runner_tasks WHERE user_id=? AND status IN ('queued','claimed')"
        args = [self.user_id]
        if kind:
            sql += " AND kind=?"; args.append(kind)
        with self._conn() as c:
            return c.execute(sql, args).fetchone()["n"]
```

### 11. Failed LLM scoring is stored as a real score of 50, so it passes the quality gate and autonomy min_score rules

- **Fingerprint:** `intelligence/scorer.py:score_job:llm-failure-fallback-score-50-passes-autonomy-min-score`  
- **Severity:** low (likelihood low: Several owner opt-ins must all hold: LLM scoring, the autonomy kill switch, a rule with min_score <= 50 (the UI suggests 70) and an established human-reviewed ledger (UI default 20 attempts). The owner must also have queued that specific job, and scoring for that job must have failed. — impact low: An application to a job the owner explicitly queued can be submitted in their name without the per-application review that the documented invariant and the stored reason promise. No other principal's data or authority is involved, and an adversary gains nothing beyond what direct score injection already gives. The deterministic controls still bound it: owner-only queueing, the kill switch, the ledger, answers_known (on by default), max_per_day, the daily cap of 10 under quality_first, and dry_run.)  
- **Confidence:** high — Every trace line was re-read in source, and an independent sandboxed run of the real scorer, queue and autonomy code showed the fallback score passing both gates in all five failure modes, while the unscored and low-score controls were refused.

**What it is.** When LLM scoring fails, score_job returns a made-up score of 50 with the reason 'Scoring failed — kept for manual review'. The failure can be a provider error, invalid JSON after the one repair retry, a NaN or non-numeric score, or a non-object reply. If the score key is missing, score_job also uses 50 but keeps the model's own reason text. score_jobs_batch writes that 50 to jobs.ai_score through update_score, and the row then looks the same as a real score. Two gates are meant to stop jobs with no score: quality_gate (the 'not_scored' reason) and the autonomy min_score condition (the docstring says 'an unscored job fails'). Neither ever sees None. So the queue gate (threshold 30 by default) and any enabled autonomy rule with min_score <= 50 treat the job as scored and good enough. The hardcoded auto_skip False also skips the auto-skip threshold that a real score of 50 would face. If every other rule condition holds, consider_auto_approval approves the application and Orchestrator._after_review submits it in the owner's name, without the manual review the stored reason promises.

**Root cause.** score_job's except block, and the default in int(result.get("score", 50)), replace an unknown score with the fixed value 50 and auto_skip False. score_jobs_batch then saves it as a real score through update_score. ai_score has no separate failed or unknown state, so the controls that reject unscored jobs (rule_failures with ai_score None, quality_gate's not_scored) never fire. This contradicts score_jobs_batch's own docstring, which says unscored jobs are left NULL 'rather than a fake score'.

**Intended behaviour.** A job whose scoring failed, or that got no valid finite numeric score, should keep ai_score NULL (or carry an explicit scoring-failed flag). quality_gate would then reject it as not_scored and every autonomy min_score rule would refuse it, leaving the job for a person, as the autonomy docstring and the fallback reason both say.

**Source path.**

- `server/routes/jobs.py:46` (entrypoint, run_pipeline) — The owner (require_owner) starts the pipeline. It inserts the scraped jobs and calls score_jobs_batch on the new ones (tracking/pipeline.py:122).
- `intelligence/scorer.py:154` (propagation, score_job) — int(result.get("score", 50)) turns a missing score into 50. NaN, non-numeric values and non-dict replies raise inside the try block.
- `intelligence/scorer.py:167` (propagation, score_job) — The except block returns score 50 and auto_skip False with the reason 'Scoring failed — kept for manual review'.
- `intelligence/scorer.py:307` (propagation, score_jobs_batch) — jobs_db.update_score stores 50 as the job's ai_score, the same as a real score.
- `applier/queue_worker.py:112` (propagation, quality_gate) — 50 is at or above the threshold (30 by default), so the job passes when the owner queues it (queue_worker.py:167) and again when the worker picks it up (queue_worker.py:321).
- `applier/autonomy.py:81` (propagation, rule_failures) — The None check never fires: ai_score 50 meets any rule with min_score <= 50.
- `applier/autonomy.py:228` (propagation, consider_auto_approval) — The pending attempt is closed as auto_approved, the step is logged and the matching rule is returned.
- `applier/session.py:749` (sink, Orchestrator._after_review) — self.submit(None, emit, actor="autonomy") submits the application without a human review.

**Lower-trust principal.** No attacker principal is needed or gains anything. A dependency failure, such as a provider outage or a malformed model reply, makes an owner-configured autonomy safety condition fail open. Scraped job text could provoke a malformed reply, but it could just as well push the model to a high score directly, so the fallback gives an adversary nothing extra. This is a fail-open safety-control defect, not a crossed principal boundary.

**Conditions.**

- *system_configuration*: The owner has turned on enable_llm_scoring (off by default) and the autonomy kill switch (off by default). The owner has an enabled rule for the site type or '*' with min_score <= 50 (the UI default for a new rule is 70). The auto-skip and quality threshold is <= 50 (default 30).
- *data_state*: The rule's ledger conditions are met: at least min_attempts human-reviewed attempts (UI default 20) and the success rate. If require_all_answers_known is set (the default), every review-packet field must come from the answer bank or profile at confidence >= 0.9. max_per_day and the daily apply limit must have room.
- *user_interaction*: The owner queued the job, an owner-only action. The UI shows its score as 50, and the side panel shows the reason 'Scoring failed — kept for manual review' (except when the score key was missing).
- *third_party_dependency*: LLM scoring fails for that job: a provider error, invalid JSON twice, or a reply without a valid numeric score.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `LLM.chat_json mocked to raise RuntimeError('LLM call failed (dummy): 503')`
- Input: `LLM.chat_json mocked to raise ValueError('LLM returned invalid JSON: dummy')`
- Input: `LLM.chat_json mocked to return json.loads('{"score": NaN, "reason": "r"}')`
- Input: `LLM.chat_json mocked to return {"reason": "no score key"}`
- Input: `LLM.chat_json mocked to return ["not","a","dict"]`
- Input: `Autonomy rule {"site_type":"*","min_score":50,"max_per_day":5,"min_attempts":1,"min_success_rate":0.9,"require_all_answers_known":false}`
- Copy the source to the verifier scratch and run it in the sandbox: no network, uid nobody, empty env except dummy APP_ENCRYPTION_KEY and APP_SECRET_KEY, prlimit. Use a temp jobs.db per mode. Add one dummy greenhouse job and one control job through an unscoped JobsDB, as tracking/pipeline.py does.
- Patch scorer._llm_scoring_enabled to True and _auto_skip_threshold to 30. Use settings quality_first=true, dry_run=true and auto_skip_score_threshold=30. Mock LLM.chat_json for each failure mode and call score_jobs_batch([job], profile, jobs_db=db).
- Read the rows back through JobsDB(user_id=1). Call queue_worker.quality_gate and queue_jobs on both jobs.
- Turn on AutonomyStore(1), add the rule above, and seed one approved_unchanged greenhouse attempt. Call autonomy.record_review_reached and then consider_auto_approval in dry run, which makes no submission. Do the same for the never-scored control job. As a positive control, also run a mode where the mock returns a real score of 20.
- Harness output is in work/evidence/v14_scorer_failopen.json (scratch-relative, to be promoted by the parent).

**Observed:** In all 5 failure modes the stored ai_score was 50 and the job stayed in stage found. The reason was 'Scoring failed — kept for manual review', except for the missing-key mode, which kept the model's reason. quality_gate returned None, queue_jobs queued the job, and consider_auto_approval returned rule id 1 (auto-approved, dry run). The never-scored control job (ai_score NULL) was rejected as not_scored and refused by autonomy (None). A real score of 20 was auto-skipped, rejected below_threshold and refused by autonomy.

**Smallest fix.** Make scoring failures fail closed. score_job should return score None plus a failed flag when the call raises, or when the reply is not a dict with a finite numeric, non-bool score (check with math.isfinite before clamping). score_jobs_batch should then skip update_score, or store NULL with a failure reason, so quality_gate returns not_scored and rule_failures refuses the job. Regression test: with chat_json raising or returning no or an invalid score, ai_score stays NULL, quality_gate returns not_scored, and consider_auto_approval returns None.

`intelligence/scorer.py`:
```python
        raw = result.get("score") if isinstance(result, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ValueError("LLM returned no valid score")
        score = max(0, min(100, int(raw)))
        ...
    except Exception as e:
        logger.warning(f"  [Scorer] Failed for {job.get('title','?')}: {e}")
        return {"score": None, "reason": "Scoring failed — kept for manual review",
                "pros": [], "cons": [], "auto_skip": False, "failed": True}

# in score_jobs_batch:
            if scored is None or scored.get("failed"):
                results.append(job)   # stays unscored (ai_score NULL)
                continue
```

### 12. GET /api/apply/limits shows non-owner users the owner's application count and last-apply timing

- **Fingerprint:** `server/routes/apply.py:get_limits:unscoped-JobsDB-cross-user-count`  
- **Severity:** low (likelihood medium: It takes one GET from any non-owner session, but it needs a multi-user install with ALLOW_SIGNUP=true. The timing signal exists only inside the short min_delay window. — impact low: It leaks only aggregate metadata about the owner's job-search activity (applications today, and recency of the latest one within the min-delay window) plus the owner's limit and dry-run settings. It exposes no job contents, PII or secrets, and allows no change.)  
- **Confidence:** high — Source shows the unscoped query with no route-level owner or user dependency. An independent sandboxed TestClient run with two dummy users showed the non-owner's response change from 0 to the owner's count of 3, with wait_seconds 44, while the non-owner's scoped views stayed at 0.

**What it is.** GET /api/apply/limits needs only a valid session. It has no require_owner and no get_current_user_id dependency. The route builds an unscoped JobsDB(), so count_applied_today() and get_last_applied_at() run over every user's rows. On a multi-user install (ALLOW_SIGNUP=true), any signed-in non-owner can read how many applications the owner submitted today. For the min_delay window after the owner's latest submission (45 s by default), the caller can also read wait_seconds, which tells it how long ago that submission happened. The same route returns the owner's machine-wide settings (daily_limit, min_delay, dry_run). The caller's own scoped views (/api/stats and /api/queue/status, which use JobsDB(user_id=uid)) correctly show 0 for that caller, and the owner's job itself returns 404 to them. _enforce_rate_limits (line 216) has the same unscoped construction, but it is reached only from the require_owner submit route, so it discloses nothing to non-owners.

**Root cause.** In server/routes/apply.py, get_limits (line 269: `jobs_db  = JobsDB()`) builds the jobs view with no user_id. JobsDB._and (db/jobs_db.py:155) then returns an empty filter, so the COUNT and MAX(applied_at) queries (db/jobs_db.py:660, 671) cover every tenant's rows. The result is returned to any session holder.

**Intended behaviour.** db/jobs_db.py:23-31 describes JobsDB() with no user_id as legacy single-user mode that 'has NOT been migrated to per-user isolation'. Per-user job data, and anything derived from it, should be read through JobsDB(user_id=uid), as /api/queue/status (server/routes/queue.py:98-99) and /api/stats already do. Either this route should be owner-only, since only the owner can submit (submit uses require_owner at apply.py:158), or it should return the caller's own scoped numbers.

**Source path.**

- `server/routes/apply.py:263` (entrypoint, get_limits) — `async def get_limits():` (registered by @router.get("/api/apply/limits") at line 262). It has no require_owner or user dependency, so only the global session gate applies: anonymous callers get 401 and any signed-in user gets 200.
- `server/routes/apply.py:269` (propagation, get_limits) — jobs_db = JobsDB() is built with user_id None (unscoped legacy view).
- `db/jobs_db.py:155` (propagation, JobsDB._and) — Returns ("", ()) when user_id is None, so the queries that follow add no user filter.
- `server/routes/apply.py:290` (sink, get_limits response) — applied_today, computed over all users' rows, goes back in the JSON response. wait_seconds (line 293, derived at line 284 from the global last applied_at) goes back with it.

**Lower-trust principal.** A signed-in non-owner account (id 2) on a multi-user install.

**Conditions.**

- *system_configuration*: ALLOW_SIGNUP=true (or a second account exists), so that a non-owner account can sign in.
- *authentication_level*: Any valid non-owner session. No owner role is needed; anonymous requests get 401.
- *data_state*: The owner has submitted applications today, or recently for wait_seconds to be non-zero, which needs the request to fall inside the min_delay window (45 s by default).

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `owner signs up first (id 1); set ALLOW_SIGNUP=true; bob signs up (id 2, owner:false)`
- Input: `three dummy jobs added for the owner via JobsDB(user_id=1).add, then the owner calls POST /api/applications/{id}/transition {"to":"submitted"} for each`
- Input: `bob: GET /api/apply/limits`
- Copy the target to scratch and run /mnt/work/v05_limits.py inside sbx.sh with a fresh APP_ENCRYPTION_KEY and a 40+ char dummy APP_SECRET_KEY. The script uses FastAPI TestClient against create_app with temp users/settings/jobs DBs, mirroring tests/test_http_auth.py setUp.
- Compare bob's /api/apply/limits before and after the owner's submissions with bob's own /api/stats and /api/queue/status. Confirm bob gets 404 for the owner's application and an anonymous client gets 401.
- Evidence file: work/evidence/v05_apply_limits_cross_user.json (promoted to agents/v05/artifacts/work/evidence/v05_apply_limits_cross_user.json).

**Observed:** bob before: /api/apply/limits applied_today 0, wait_seconds 0. After the owner's three submissions, bob's GET /api/apply/limits returns 200 {daily_limit:10, applied_today:3, daily_limit_reached:false, min_delay:45, wait_seconds:44, dry_run:false}, identical to the owner's response. bob's /api/stats applied_today is 0, bob's /api/queue/status applied_today is 0, bob's GET /api/applications/<owner job> returns 404, and an anonymous GET /api/apply/limits returns 401.

**Smallest fix.** Since only the owner can submit and the limits describe the owner's machine-wide applying, put GET /api/apply/limits behind require_owner. Alternatively, scope both readers to the session user. For consistency, also scope _enforce_rate_limits to the submitting uid; this is equivalent for the owner, because NULL legacy rows are adopted by user 1 (db/jobs_db.py:284).

`server/routes/apply.py`:
```python
@router.get("/api/apply/limits")
async def get_limits(uid: int = Depends(require_owner)):
    ...
    jobs_db = JobsDB(user_id=uid)
    ...

def _enforce_rate_limits(uid: int):
    ...
    jobs_db = JobsDB(user_id=uid)   # called as _enforce_rate_limits(uid) from submit()
```

### 13. Generic settings reads show the owner's private notification addresses and the last 4 characters of secrets to non-owners

- **Fingerprint:** `server/routes/settings.py:settings-reads-expose-owner-notify-addresses`  
- **Severity:** low (likelihood low: Needs ALLOW_SIGNUP=true and a second signed-in account. Signup is closed by default once an owner exists. — impact low: Exposes the owner's personal notification addresses, SMTP username and host, and Telegram chat id. Also exposes the last 4 characters of API keys, the SMTP password and the bot token. No complete credential is exposed and nothing can be changed.)  
- **Confidence:** high — Reproduced independently in the OS-enforced sandbox with dummy data. In the same run, the owner-only sibling route returned 403 to the same non-owner, while both generic routes returned the protected values.

**What it is.** server/routes/notifications.py says the notify_* settings are owner-only because 'their addresses are personal', and GET /api/notifications/settings uses require_owner. A non-owner gets 403 there. But GET /api/settings (server/routes/settings.py:28-38) returns every row of the same SettingsDB table to any signed-in user. It masks only is_secret_key values, and those keep their last 4 characters. So notify_email_to, notify_email_from, notify_smtp_username, notify_smtp_host and notify_telegram_chat_id come back in plaintext. GET /api/settings/raw/{key} (lines 49-61) returns any non-secret key by name, such as notify_email_to. Neither route has require_owner. The global auth gate (server/app.py:77-95) only checks for a valid session. So any signed-in non-owner can read the owner's notification inbox address, SMTP username and host, and Telegram chat id. The same user also sees the last 4 characters of every secret, which covers API keys, the SMTP password and the Telegram bot token.

**Root cause.** get_settings and get_setting_raw in server/routes/settings.py have no require_owner dependency and no per-key owner filter. They only mask secret values (keeping the last 4 characters) or refuse them (raw). SettingsDB.read_all returns every row decrypted. So the notify_* keys that notifications.py protects with require_owner can also be read through this second, weaker path to the same rows.

**Intended behaviour.** Only the owner should be able to read notify_* settings, as GET /api/notifications/settings already enforces. For non-owners, the generic settings reads should leave out the notify_ prefix and the secret keys, or both routes should require the owner.

**Source path.**

- `server/routes/settings.py:28` (entrypoint, get_settings) — The GET /api/settings route. Its signature has no require_owner dependency, so any session from the global auth gate gets through.
- `db/settings_db.py:351` (propagation, SettingsDB.read_all) — Returns every row of the settings table, decrypted, including the notify_* rows written by PUT /api/notifications/settings.
- `server/routes/settings.py:35` (propagation, get_settings) — Masks only is_secret_key values and keeps their last 4 characters. notify_email_to, notify_smtp_username, notify_smtp_host and notify_telegram_chat_id go out unmasked.
- `server/routes/settings.py:60` (sink, get_setting_raw) — /api/settings/raw/{key} reads any non-secret key, such as notify_email_to, and returns it verbatim to any signed-in user.

**Lower-trust principal.** A signed-in non-owner (user id 2, owner=false)

**Conditions.**

- *system_configuration*: ALLOW_SIGNUP=true, so a second, non-owner account can exist (server/routes/auth.py).
- *authentication_level*: The attacker has any signed-in non-owner session (user id other than 1).
- *data_state*: The owner has configured notification settings, such as an email recipient or a Telegram chat id, or API keys and secrets.

**Bounded reproduction (already performed in the sandbox; do not run against a live instance).**

- Input: `GET /api/notifications/settings`
- Input: `GET /api/settings`
- Input: `GET /api/settings/raw/notify_email_to`
- Input: `GET /api/settings/raw/notify_telegram_chat_id`
- Copy the source into the sandbox scratch. Run it with a fresh Fernet APP_ENCRYPTION_KEY and a dummy APP_SECRET_KEY of 40 or more characters. Point SettingsDB, UsersDB and JOBS_DB_PATH at temp paths, as in tests/test_http_auth.py setUp, and use create_app(ui_dir=<missing dir>) with TestClient.
- Sign up owner-dummy@example.com (user 1). Set cfg.ALLOW_SIGNUP=True and sign up bob-dummy@example.com with a second client (user 2; /api/auth/me shows owner=false).
- As the trusted harness, write these dummy values with SettingsDB.write: notify_email_to=owner-inbox-dummy@example.com, notify_telegram_chat_id=123456789, notify_smtp_username=owner-smtp-dummy, notify_smtp_host=smtp.dummy.invalid, openai_api_key=sk-dummy-owner-key-ABCD and notify_smtp_password=dummy-pass-WXYZ.
- As user 2, request each payload. The output is saved to scratch work/evidence/settings_notify_nonowner.json, to be promoted to agents/v10/artifacts/work/evidence/settings_notify_nonowner.json.

**Observed:** User 2: GET /api/notifications/settings returned 403 (the owner got 200). GET /api/settings returned 200 with notify_email_to='owner-inbox-dummy@example.com', notify_telegram_chat_id='123456789', notify_smtp_username='owner-smtp-dummy', notify_smtp_host='smtp.dummy.invalid', openai_api_key='••••••••ABCD' and notify_smtp_password='••••••••WXYZ'. GET /api/settings/raw/notify_email_to and /api/settings/raw/notify_telegram_chat_id returned 200 with the dummy values. The raw route returned 403 for notify_smtp_password and openai_api_key.

**Smallest fix.** Make the generic settings reads respect the owner-only boundary that notifications.py declares. For non-owners, leave notify_* keys and secret keys out of GET /api/settings and refuse notify_* keys in /api/settings/raw/{key}. Alternatively, make both routes require the owner and give the providers list its own non-owner-safe endpoint. Add a regression test that a second user cannot see any notify_* value or secret suffix through any settings route.

`server/routes/settings.py`:
```python
from core.auth import get_current_user_id, require_owner, OWNER_USER_ID

_OWNER_ONLY_PREFIXES = ("notify_",)

@router.get("/api/settings")
async def get_settings(uid: int = Depends(get_current_user_id)):
    db = SettingsDB.get(); masked = {}
    for k, v in db.read_all().items():
        if uid != OWNER_USER_ID and (k.startswith(_OWNER_ONLY_PREFIXES) or is_secret_key(k)):
            continue
        masked[k] = ("••••••••" + v[-4:] if len(v) > 4 else "••••") if is_secret_key(k) and v else v
    return JSONResponse({"settings": masked, "provider_info": db.provider_info()})

@router.get("/api/settings/raw/{key}")
async def get_setting_raw(key: str, uid: int = Depends(get_current_user_id)):
    if is_secret_key(key) or (uid != OWNER_USER_ID and key.startswith(_OWNER_ONLY_PREFIXES)):
        raise HTTPException(403, "Not readable")
    return JSONResponse({"key": key, "value": SettingsDB.get().read(key, "")})
```

## 5. Needs validation (no severity assigned)

| Title | Repository trace | Exact blocker | Bounded local next step | Owner-observed deployment check |
|---|---|---|---|---|
| Resume text extraction decodes PDF content streams with no limit on decoded size | `server/routes/kits.py:50` → `db/resumes_db.py:200` → `db/resumes_db.py:143` | Whether this is a meaningful denial of service for other users rather than the graceful self-recovery observed depends on the deployed host's RAM, swap, kernel overcommit and OOM-killer target selection, the asyncio.to_thread pool concurrency, and any reverse-proxy request-body cap (the /api/setup/upload-resume path has no app-level cap at all) — none of which are visible in the repository. Observ… | Completed in the parent-approved sandbox (1 GiB address-space limit, uid nobody, no network, read-only target copy, scratch-only writes). A benign, highly-compressible single-page PDF of 510,229 bytes (valid %PDF- signature; passes db.resumes_db.check_pdf; well under the 5 MB cap) was fed to the exa… | The owner checks whether the server process runs under a memory cgroup or ulimit, the host RAM/swap and kernel overcommit/OOM-killer configuration (whether the uvicorn process would be the OOM victim), the asyncio default thread-pool size, and whether a reverse proxy caps request bodies (especially … |
| Runner screenshot endpoint spools unlimited multipart bodies to disk before the runner token or the 3 MB cap is checked | `server/static_shell.py:34` → `server/app.py:80` → `server/routes/runner.py:70` → `server/routes/runner.py:73` | Whether the deployed reverse proxy, which .env.example recommends putting in front (Caddy/nginx, comment above APP_HOST), caps request body size for /api/runner/*. nginx defaults to 1 MiB (client_max_body_size). Caddy has no default limit. Whether the uvicorn port or the proxy is reachable by untrusted clients. APP_HOST defaults to 127.0.0.1 (core/config.py), but the remote runner documented in ru… | Already done by verifier v09 in the OS-enforced sandbox (no network, scratch-only writes, prlimit). The script /mnt/work/v09_spool.py (scratch) wraps starlette.formparsers.SpooledTemporaryFile to count bytes written and track rollover. It drives create_app() directly over ASGI with a 6 MiB multipart… | The owner inspects, without sending traffic to the live deployment: (1) the ingress configuration in front of uvicorn, for a request-body limit that applies to /api/runner/* (nginx client_max_body_size in the relevant server/location block, Caddy request_body { max_size }), and any concurrency or ti… |

Full text of each lead is in `NEEDS-VALIDATION.md`.

## 6. Hardening notes and positive patterns

Rejected candidate (kept so a future run does not re-raise it without new evidence): `applier/universal.py:run_application:pre-review-fill-and-resume-upload-into-unverified-apply-landing-origin` — The trace is an accurate description of the code, but it does not establish a crossed security boundary; it describes the intended operation of an auto-apply tool, and the one hard control it defines remains enforced. (1) The claimed invariant — that the origin the automation fills must be bound to the job listing's origin — is not a boundary this system establishes or can feasibly establish. Handing a job listing off to an application form on a different origin is the normal, ubiquitous case (LinkedIn/Indeed listings legitimately redirect to Greenhouse, Lever, Workday, iCIMS, SmartRecruiters …

### Hardening (not findings: no crossed boundary, or owner-only input)

- Autonomy rule selection uses the site type detected from page HTML markers (applier/universal.py _site_type -> applier/site_type.py _EMBEDDED_ATS). A generic careers page whose HTML contains a string such as 'smartrecruiters.com' is therefore judged under the owner's SmartRecruiters rule and counted in that site type's ledger stats. Consider binding the autonomy site type to the URL host, or to the URL-only type that apply_mode already uses.
- The autonomy min_score condition uses ai_score, which an LLM computes from the attacker-written job description. Treat it as a ranking signal, not an authorization gate, and keep the deterministic conditions (require_all_answers_known, max_per_day, the daily limit) on by default for '*' rules.
- applier/form_analyzer.py analyze() sends up to 2000 characters of the whole non-empty _flat profile, including identity fields such as ethnicity, disability_status and date_of_birth, to the LLM for every form page. Sending only the profile keys that match extracted field labels would reduce what a manipulated analysis can place into fields.
- applier/form_filler.py _fill_one radio branch builds f"{sel}[value='{value}']" from LLM output without CSS escaping. This is not a security boundary (it stays on the same page), but escaping with _css_escape_attr would avoid misclicks.
- core/browser.py:88 and runner/runner.py:441 enable accept_downloads=True although no code ever consumes a download; set accept_downloads=False (Chromium then denies downloads outright) or attach a page.on('download', lambda d: d.cancel()) handler in applier/executors.py ServerExecutor and runner/runner.py launch, so an untrusted ATS page cannot stream unbounded bytes into the host's temp directory for the lifetime of a pass (up to ~30 min on a runner waiting for a human). Regression case: a loopback fixture page that clicks three <a download> links; assert no file appears under tempfile.gettempdir()/playwright-artifacts-* while the pass runs.
- Document in README/runner docs that Playwright redirects downloads away from the Opera/runner profile's real Downloads folder into a per-launch temp folder that is deleted on context close, so operators do not expect to find or need to clean downloaded files.
- core/browser.py launch_browser: drop --no-sandbox (it is unnecessary on a desktop profile and removes the renderer sandbox for every untrusted ATS page); do not strip --disable-extensions/--disable-sync/--disable-background-networking/--disable-default-apps; set accept_downloads=False (or a downloads_path with a reject handler) since no code path consumes downloads.
- core/browser.py: launch the server automation in a dedicated profile directory (e.g. data/browser_profile) as runner/runner.py already does, and copy only the scoped encrypted site cookies (load_cookies) into it instead of exposing the owner's entire Opera profile (all site sessions, passwords, extensions).
- applier/universal.py _click_apply_button / run_application: after the Apply click, compare the landing page's origin with the job URL host or an allowlist of known ATS hosts (applier/site_type.py already knows them) and stop with a review-needed state on mismatch; also validate the job URL scheme is http(s) before page.goto rather than relying on preflight, which ignores validate_base_url failures.
- applier/universal.py run_application: defer resume set_input_files (and ideally all typing) to the post-approval submit pass, or at least gate the file upload on review approval, so the documented human-review-before-anything-happens guarantee holds for form-based platforms as it does for CLICK_IS_TERMINAL ones.
- runner/runner.py launch: same extension/sync/downloads flags as above; the dedicated profile is good but the page still runs with extensions enabled by default.
- core/browser.py launch_browser: the 'tasklist' shell check (line 39) is Windows-only and silently no-ops elsewhere; on other platforms an already-running Opera on the same profile will make launch_persistent_context fail with an unclear error.
- GET /api/settings (server/routes/settings.py:35) returns a dotted prefix plus the last 4 characters of each DECRYPTED secret (API keys, notify_smtp_password, notify_telegram_bot_token). Last-4 masking is industry-standard for high-entropy keys but is still cleartext secret material in a client-visible response; for user-chosen secrets like an SMTP password consider masking with no trailing cleartext.
- POST /api/auth/logout (server/routes/auth.py:82) only clears the cookie and does not bump session_version, so a signed session token obtained out-of-band remains valid for its full 14-day max_age after the user logs out; change-password and logout-everywhere do revoke server-side. HttpOnly+Secure limit token capture, but bumping session_version on logout would give parity and true revocation.
- WebSocket /ws/{job_id} (server/websocket.py:57) authenticates the session and enforces job ownership but performs no Origin allowlist check; cross-origin hijack is currently prevented only by the SameSite=Lax session cookie not being attached to cross-site WS handshakes, and the handler is read-only. An explicit Origin/Host check would add defense in depth independent of browser SameSite behavior.
- The application has no CSRF token and no Origin/Referer validation on any route (server/app.py); CSRF protection relies solely on the explicit SameSite=Lax session cookie (core/auth.py:145). This is effective for the non-GET JSON/multipart mutations actually used, but it is a single layer — an anti-CSRF token or Origin check on state-changing routes would harden against future SameSite regressions or a route added with a Lax-permitted method.
- The session throttle (core/throttle.py) is in-memory and per-process and the login IP is request.client.host; behind a reverse proxy without uvicorn --proxy-headers/--forwarded-allow-ips every client shares one bucket. This is documented in the module and is a deployment-config concern, not a source defect.
- core/urlcheck.py validate_base_url (104-107) accepts hosts written with IDNA label separators (U+3002, U+FF0E, U+FF61), e.g. http://169。254。169。254/; httpx/OpenAI connect to 169.254.169.254 (owner-only input). Fix: reject non-ASCII hosts or IDNA/UTS46-encode before the IP/blocklist checks.
- Redirects are followed without revalidation: applier/preflight.py fetch_page (urllib) and the OpenAI clients in core/llm.py (follow_redirects=True). Fix: a redirect handler that revalidates, or httpx.Client(follow_redirects=False).
- applier/universal.py page.goto(job['url']) (111, 247, 342) has no scheme/host check; add the same http(s)/no-metadata check as preflight.
- validate_base_url does not check DNS resolution (documented at core/urlcheck.py:16-19); a connect-time check in a custom transport would close it and the redirect gap.
- frontend/src/components/applications/Workspace.jsx:33 and SidePanel.jsx:96/116: the ?job= query value goes unencoded into fetch paths, which is a client-side path traversal primitive (../../../api/settings?x= resolves to /api/settings). The only reachable unsafe request today is a victim-clicked Notes POST with the fixed body {note}, which has no security effect. Wrap jobId in encodeURIComponent, or validate ^[0-9a-f]{16}$ before setOpenId, so a future GET+POST endpoint cannot be reached this way.
- server/app.py sets no X-Frame-Options / CSP frame-ancestors. Clickjacking is currently blocked for cross-site framers by the SameSite=Lax session cookie (core/auth.py:145), but a same-site origin (e.g. another port on the same host) could frame the Approve/Submit UI. Add 'Content-Security-Policy: frame-ancestors none' (and X-Frame-Options: DENY) in a response middleware.
- templates/setup.html:243 builds row.innerHTML from interpolated values with no escaping. It is not served today; delete the file so it cannot be re-mounted.
- safeHref (frontend/src/lib/live.js:65) correctly limits job links to http(s), but any http(s) destination is allowed. That is acceptable because every anchor uses rel=noopener noreferrer.
- dry_run is parsed inconsistently: applier/queue_worker.is_dry_run strips whitespace but applier/session.Orchestrator._dry_run_enabled and server/routes/apply._settings_dry_run do not, and POST /api/settings stores dry_run verbatim. A value such as 'true ' lifts the daily limit in the worker and autonomy while the Orchestrator really submits. Normalise dry_run to true/false on save (like quality_first) and parse it with one helper. Only the owner can write it, and the UI writes exact values.
- server/routes/apply._enforce_rate_limits uses unscoped JobsDB(), so the owner's daily limit and min-delay count every user's applied_at rows. There is no production path for a non-owner to create jobs today (only the owner-only pipeline inserts), so this cannot be exploited now. If per-user job creation is ever added, a non-owner could exhaust the owner's submit limit. Scope it to the owner.
- daily_apply_limit and min/max_delay_between_applies are not validated on POST /api/settings. A negative daily_apply_limit with quality_first off behaves as unlimited (cap > 0 check). Validate them as non-negative integers.
- The /api/apply/submit cap and min-delay check is check-then-act. Parallel owner submits for different job ids (each takes only its own per-job lock) can all pass before any completes, and autonomy submits never check min_delay_between_applies. Owner-driven only.
- POST /api/applications/{id}/transition (session-only) lets any user move their own job found->queued or ->preparing, which bypasses the owner-only queue route and the quality gate. The worker reads only the owner's queue and re-checks the gate before claiming, so nothing runs. Consider restricting user-initiated targets to the stages the UI needs.
- JobsDB.clear_all leaves job_notes and job_briefs (default branch), plus application_kits, autonomy_attempts and runner_tasks (both branches), keyed by job_id. Re-scraping the same URL recreates the same unscoped job_id and reattaches the old notes, brief and kit for the same owner. Delete or tombstone them with the job.
- ai_score comes entirely from an LLM reading attacker-authored job descriptions (intelligence/scorer.py:136-150) with no deterministic cross-check, yet autonomy uses it as an approval condition. Consider labelling min_score as advisory in the UI, or requiring require_all_answers_known (already the default) and an explicit per-site rule so an inflated score alone never widens auto-submission.
- score_job treats a string auto_skip such as "false" as truthy (scorer.py:158). This fails closed (the job is skipped), but bool parsing should be strict.
- tracking/pipeline.py dedups with an unscoped JobsDB (exists_bulk and add check url_hash across all users). Today only the owner pipeline adds jobs, but a future per-user job import would let one tenant suppress the owner's scraped jobs.
- Host / X-Forwarded-Host supplied by the client is also relayed to the backend in --dev-ui mode, but no backend route derives absolute URLs, redirects, password-reset links, or cache keys from it (only request.url.path and request.url.query are read) and set_session_cookie sets no cookie Domain, so the host-header trust itself has no confirmed security impact; only the X-Forwarded-For -> throttle path is exploitable.
- LoginThrottle is in-memory and per-process (core/throttle.py): it resets on restart and is not shared across uvicorn workers, so with multiple workers each bucket's effective limit multiplies by the worker count — acknowledged in source as acceptable for the single-process default, but worth a Redis-backed store before scaling out.
- LoginThrottle._MAX_TRACKED_KEYS eviction (core/throttle.py:65-73) drops the oldest tracked key when the table is full; a client churning many distinct X-Forwarded-For values can evict a legitimate victim's accumulated failure counters, which only further loosens the limits — bounded memory, no DoS, but reinforces that the per-IP dimension is not a reliable control once XFF is spoofable.
- requirements.txt pins only >= floors and has no lockfile or hashes. Pin exact versions with hashes, e.g. pip-compile --generate-hashes, so a fresh install cannot pull a newer, untested or compromised release.
- No security response headers are set: no CSP, no X-Frame-Options or frame-ancestors, no X-Content-Type-Options. SameSite=Lax keeps the session cookie out of cross-site frames, but same-site subdomains could still frame the UI. Add frame-ancestors 'none' and a basic CSP.
- On a fresh install, whoever submits the first POST /api/auth/signup becomes owner (id 1) with no setup token. That is safe on the default 127.0.0.1 bind, but if the app is exposed before the operator signs up, a network attacker can claim the owner account. Consider a one-time bootstrap token printed to the console.
- Any signed-in user, non-owners included, can read /docs and /openapi.json, which list the full API. Disable docs_url/openapi_url in production, or gate them behind require_owner.
- runner/runner.py takes the bearer token as a --token command-line argument, so other local users can see it in the process list, and it accepts http:// servers with only a warning. Read the token from an env var, a file or stdin, and refuse non-loopback http.
- The .gitignore covers .env and *.env.local but not other .env.* names (e.g. .env.production), nor loose *.pem or *.key outside data/.
- clear_session_cookie calls delete_cookie without the matching Secure/SameSite attributes. Harmless today, since the name and path match, but keep the attributes consistent.
- On Windows, ApplicationSession.load(job_id) builds SESSIONS_DIR / f'{job_id}.json' straight from the path parameter, so a backslash in the id could point outside the sessions directory. Today it reads only session-shaped JSON and gives the same 404 for every failure, but validating job_id against ^[0-9a-f]{16}$ before using it in a path would remove the risk.
- Changing the password or bumping session_version (db/users_db.py change_password) revokes browser sessions but not runner tokens. Consider revoking or flagging runner tokens on password change.
- RunnerTokenStore.revoke does not release tasks claimed by the revoked token. They stay claimed until the 120 s lease lapses, and a submit task then becomes lost_submit (needs_you).
- clean_result always sets the dry_run key, so Orchestrator.finish_pass lets the runner's reported dry_run=false override a submit task created with dry_run=true. The job is then recorded as submitted and counted against the daily limit. The server should not let a result turn a dry-run task into a real submission.
- No owner action (skip or transition) cancels a claimed runner task, and finish_pass does not re-check the job stage before running the finishers. A runner's heartbeats keep getting 200 after the owner skips the application, so the runner never gets the 404/409 'drop it' signal it supports, and its result is still processed.
- Check-then-act on MAX_SCREENSHOTS_PER_TASK and MAX_EVENTS_PER_TASK, and add_screenshot's read-modify-write of the JSON list without BEGIN IMMEDIATE, allow concurrent uploads to exceed the cap and lose list entries, leaving untracked files. Belongs to the peer unit for runner screenshot resource handling.
- The runner client does not check the format of server-supplied job_id and task_id (job_id becomes part of a local screenshot path via applier/screenshotter.py), and it does not re-check that application.url is http(s). Validating hex ids and the scheme client-side would add defence in depth.
- The runner only warns when --server is plain http on a non-loopback host, so the bearer token travels unencrypted.
- In --dev-ui mode, main.py starts `next dev` with no hostname flag. Next's dev server normally listens on all interfaces and proxies /api/* to the backend (frontend/next.config.mjs), which can put the API on the LAN even with APP_HOST=127.0.0.1. Not validated: frontend/node_modules is missing and the Next default is outside the repo. Pass `-H 127.0.0.1` in the dev script.
- There is no app-wide request-body cap: uvicorn sets none, and public /api/auth/* JSON bodies are fully buffered in memory before validation. Add a body-size middleware, or document a required proxy limit that still allows 3 MB screenshots and 5 MB resumes.
- /api/setup/upload-resume truncates resume.pdf in place (open 'wb'). A form fill or runner download reading it at the same time can see a partial file. Use a temp file plus os.replace.
- Neither StaticFiles mount adds X-Content-Type-Options: nosniff. Runner PNGs are checked only up to IHDR, but they are served as image/png, so no exploitable sniffing was found.
- DELETE /api/jobs (JobsDB.clear_all) and account deactivation leave data/sessions/<job_id>.json and data/screenshots/<job_id>_*.png on disk, and they stay reachable. Consider deleting derived copies together with the job.
- Server-side screenshot names in applier/screenshotter.py:16 are {job_id}_{label}_{HHMMSS}.png (low entropy). Use random suffixes as applier/runner_hub.py:446-449 does.
- GET /api/settings/login-status, GET /api/status and GET /api/setup/checklist show non-owners the owner's job-site login state, LLM provider, dry_run and profile completeness. This is low-sensitivity owner state with no owner check.
- GET /api/settings masks secret keys but keeps the last 4 characters (server/routes/settings.py:35). This is owned by the peer crypto/secrets unit and noted here only as a variant.
- _save_answers writes every key in the request's answers object, not only keys of this application's pending prompts. Unlike POST /api/profile it does not check keys against known fields or cap value length, so arbitrary custom profile rows can be created. Owner-only, so this is a same-principal issue, but the recommended prompt_keys filter closes it.
- runner_hub.clean_result limits a runner-reported missing field_key to 100 characters but does not check its value against profile keys. It feeds the same _save_answers/resolve_missing sink and is closed by the same allow-list.
- ProfileDB.missing_fields rows carry no user_id and resolve_missing looks up by row id alone. Today only the owner's session prompts supply ids, but scoping the row to job_id/user would make the binding explicit.
- server/routes/onboarding.py upload_resume overwrites cfg.RESUME_PATH in place (open 'wb' then copyfileobj) with no temp-file-and-rename and no %PDF- signature check. A failed or concurrent upload leaves a truncated or non-PDF global resume that the appliers then read. Write to a .part file, validate it, then os.replace.
- applier/runner_hub.py screenshot checks len(task['screenshots']) >= 20 before db/runner_db.add_screenshot does an unlocked read-modify-write of the JSON list, so concurrent uploads from one token can store more than 20 files and lose list entries (orphaned files on disk). Do the count check and the append in one BEGIN IMMEDIATE transaction.
- core/resume_parser._extract_text has no page or character cap of its own, while db/resumes_db.extract_text has one. Apply TEXT_PAGES/TEXT_CHARS-style limits there too, and stop iterating once enough text is collected.

### Positive patterns worth keeping
- Single auth-gate middleware; new routes are protected by default (`server/app.py`).
- `is_public_path` decides public files by what exists in the export, with traversal/symlink/NUL-safe resolution (`server/static_shell.py`).
- Session tokens re-checked for signature, age, account state and session version on every request (`core/auth.py`).
- scrypt + `hmac.compare_digest`, dummy-hash timing equalisation, per-account and per-IP login throttle (`db/users_db.py`, `core/throttle.py`).
- Parameterised SQL with allow-listed dynamic column names everywhere in `db/`.
- Secrets encrypted at rest and masked in responses; raw reads of secret keys refused (`core/crypto.py`, `server/routes/settings.py`).
- Runner tokens: 256-bit CSPRNG, stored hashed, exact-path gate bypass regex, per-task binding to the claiming token.
- Resume variants: server-chosen names, size + signature checks, atomic writes, plaintext refused on read (`db/resumes_db.py`).
- Frontend: no `innerHTML`/`eval`; `safeHref` limits links to http(s) with `rel="noopener noreferrer"`.

## 7. Coverage summary

Ledger: 10 covered, 13 candidate (all validated), 0 blocked, 0 deferred, 0 out of scope. Excluded companion domains: memory-safety (no native code), cloud/IaC (none in repo), supply-chain/CI (none; pinning reviewed under *Obvious things*), desktop/IPC (runner is an HTTP CLI; the server-side browser-profile question was reviewed under a dedicated unit). Final-clean critic result: no missing units, no reassignments.

Files: `findings.json` (machine-readable, schema-validated), `coverage-ledger.json`, `FINDINGS-DETAIL.md`, `NEEDS-VALIDATION.md`, `agents/*/artifacts/` (promoted evidence).