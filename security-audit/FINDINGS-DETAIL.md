# Findings detail (medium and above)

## A runner claim with wait=NaN never times out, spins the shared jobs DB, and still delivers a task after the token is revoked

`applier/runner_hub.py:RunnerHub.claim:nan-wait-unbounded-long-poll-survives-token-revocation` — **medium**

### Ordered trace

1. `server/routes/runner.py:51` (entrypoint, claim): POST /api/runner/claim accepts the JSON body as a plain dict. Line 52 takes wait = data.get('wait') and passes it to RunnerHub.claim in a worker thread without validating it.
2. `server/routes/runner.py:40` (propagation, require_runner): The bearer token is authenticated once, at request start. Nothing re-checks it during the long poll.
3. `applier/runner_hub.py:296` (propagation, RunnerHub.claim): wait = min(max(float(wait), 0.0), 25.0). float('nan') succeeds, and both max and min keep NaN.
4. `applier/runner_hub.py:305` (propagation, RunnerHub.claim): deadline = clock() + NaN. At lines 311-314, remaining is NaN, 'remaining <= 0' is false, and _wait(min(NaN, 1.0)) -> Condition.wait(NaN) returns at once. The result is a busy loop with no bound, calling sweep() and _claim_valid().
5. `db/runner_db.py:215` (propagation, RunnerTokenStore.revoke): Revocation only sets revoked_at. Open polls and tasks already claimed by that token are left as they are.
6. `db/runner_db.py:306` (sink, RunnerTaskStore.claim): UPDATE runner_tasks SET status='claimed', claimed_by=<revoked token_id> with no check of runner_tokens.revoked_at. RunnerHub.payload (applier/runner_hub.py:378-389) then returns the profile subset, answers and kit to the revoked holder.

### Evidence

- `applier/runner_hub.py:296` — The only validation of wait is float() plus a min/max clamp, and the clamp keeps NaN.
- `applier/runner_hub.py:312` — The loop exits only when 'remaining <= 0', which is false for NaN.
- `applier/runner_hub.py:259` — _wait calls Condition.wait(seconds). With seconds=NaN this does a non-blocking acquire and returns at once, so the loop spins.
- `applier/runner_hub.py:383` — The payload carries the owner's profile subset, plus the answer bank, application answers, kit and resume metadata (lines 384-388).
- `db/runner_db.py:302` — claim() selects and claims the oldest queued task for any token_id. It does not look at whether the token is revoked.
- `db/runner_db.py:15` — Documented invariant: a token 'can be revoked, which stops it at once'.
- `core/runner_auth.py:46` — There is one authentication call per HTTP request, and no later re-check.
- `server/routes/runner_tokens.py:41` — The revoke route only calls RunnerTokenStore.revoke. It does not end polls or touch tasks.

### Dummy attacker / affected resource
Holder of a runner token that the owner later revokes

### Native input and bounded instructions

- `POST /api/runner/claim with header 'Authorization: Bearer <apxr_ token>' and body {"wait": "nan"}`
- Copy the source into sandbox scratch and make the copy writable. In-process, set up the app the way tests/test_runner.py RunnerHttpTests does: temp settings/users/jobs DBs, a signup for the dummy owner, then the owner creates a runner token through POST /api/settings/runner-tokens.
- Baseline: POST /api/runner/claim {"wait":1} returns {"task":null} after about 1 s. Control: {"wait":1e9} is clamped and returns after about 25 s.
- From a thread, POST /api/runner/claim {"wait":"nan"} with the token, counting RunnerTaskStore.sweep calls.
- After 27 s, check that the request is still open and that a second claim with the same token gets 429.
- As the owner, DELETE /api/settings/runner-tokens/<id>. Check that a fresh claim with the token now gets 401 and that the NaN poll is still alive.
- Add a dummy job, move it to preparing, and enqueue an apply task with RunnerHub(1).enqueue. Check the pending response and the task row's claimed_by.
- Command: sbx.sh <scratch> 120 bash -c 'cd /mnt/work/src && env APP_ENCRYPTION_KEY=<fresh Fernet key> APP_SECRET_KEY=<40+ char dummy> python3 /mnt/work/v02_nan.py'. The output is written to scratch work/evidence/v02_nan_revoked.json, a predeclared file for parent promotion.

### Observed output and the invariant it proves
Independent re-run by the verifier. The wait=1 baseline returned {"task":null} after 1.01 s, and wait=1e9 was clamped and returned after 25.02 s. The NaN poll was still open after 27 s, with 80727 sweep() calls. A second poll by the same token got 429 'this token is already waiting for a task'. Revoke returned 200, a fresh claim with the token got 401, and the NaN poll was still alive 2 s later. After an apply task was queued, the NaN poll returned HTTP 200 after 29.06 s carrying that task's id and the payload keys [answers, application, attempt, kind, kit, lease_seconds, profile, resume, task_id]. The task row showed status=claimed, claimed_by=1, which is the revoked token's id, and the token list showed active=false.

Invariant violated: A claim waits at most MAX_WAIT_SECONDS (25 s). A non-numeric or non-finite wait gets a 400. The documented invariant (db/runner_db.py:15, server/routes/runner_tokens.py:8) is that a revoked token stops working at once. So a revoked token, or a token whose user is inactive, can never claim a task or receive a payload after the revocation commits, including from a poll that started earlier.

### Conditions and containment

- *authentication_level*: The attacker holds a runner bearer token that was valid when the claim request started, for example a stolen or leaked apxr_ token or a compromised runner machine. Only the owner can mint tokens.
- *data_state*: For the disclosure after revocation, the owner must queue a runner task (executor runner, or auto) while the spinning poll is alive. The availability effect (busy loop, a held thread, a write transaction on every iteration) needs no data.
- *network_routing*: To read the payload, the attacker keeps the HTTP connection open until a task arrives. The server-side spin and the claim of the task under the revoked token happen even if the client disconnects, because asyncio.to_thread work is not cancelled.

### Remediation and regression case
Reject a non-finite wait with a 400. Re-check token liveness on every loop iteration. Make the claim UPDATE itself require an unrevoked token, so a revocation committed during a poll cannot hand out a task. On revoke, return tasks claimed by that token to the queue; submit tasks go to the existing lost_submit handling instead.

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

## Answers to form questions overwrite the owner's master work-authorization, sponsorship and identity facts under keys the job page chose

`applier/session.py:_save_answers:page-derived-field-key-writes-master-profile-bypassing-CORRECTABLE_FIELDS` — **medium**

### Ordered trace

1. `applier/adapters/form.py:292` (entrypoint, FormAdapter._field): The label from the page's form control becomes field_key through _key -> standard.key_for (form.py:271-275). On the universal path the LLM's field_key is copied verbatim (form_analyzer.py:299).
2. `applier/adapters/standard.py:86` (propagation, key_for): Any label matching _AUTH (line 45), including one naming the United States, returns authorized_to_work. A label matching 'sponsor' returns require_sponsorship (line 88).
3. `applier/adapters/standard.py:125` (propagation, profile_value): A question that names a country other than location_country is deliberately not answered from the profile, so resolve() marks the required field needs_manual (line 149).
4. `applier/session.py:202` (propagation, Orchestrator._finish_first_pass): flag_missing stores the page-derived field_key, and missing_prompts carry it to the UI (line 219). The UI shows only the label (ApplyTab.jsx:88).
5. `server/routes/apply.py:146` (propagation, answer): The owner's POST /api/apply/answer/{job_id} (route at line 118) runs Orchestrator.run_iter2, which calls _save_answers first (session.py:252), before preflight or any browser work.
6. `db/profile_db.py:347` (propagation, ProfileDB.resolve_missing): Also writes the value into profile_fields under the stored page-derived field_key, with source user_prompt.
7. `applier/session.py:298` (sink, Orchestrator._save_answers): set_many writes every answered key into the global profile.db with no CORRECTABLE_FIELDS filter, unlike _apply_corrections (session.py:833).

### Evidence

- `db/profile_db.py:91` — CORRECTABLE_FIELDS excludes authorized_to_work, require_sponsorship, full_name, email, URLs, search_*, resume_summary and cover_letter. The comment at lines 69-90 says to change these only via the Profile page.
- `applier/session.py:833` — Only the corrections path filters keys against CORRECTABLE_FIELDS. The local control confirmed that it dropped authorized_to_work and email.
- `applier/form_analyzer.py:299` — The universal filler copies field_key verbatim from the LLM output.
- `applier/adapters/standard.py:29` — STANDARD_KEYS includes authorized_to_work and require_sponsorship. resolve() then fills later forms from the overwritten master value without asking.
- `db/answer_bank_db.py:447` — Profile-derived answer-bank entries also serve the overwritten profile value as candidates for matching questions.
- `applier/review.py:131` — A profile hit is labelled source profile at CONF_PROFILE=0.95 (line 46), so the review packet does not flag the value as unusual.
- `frontend/src/components/applications/ApplyTab.jsx:88` — The prompt shows p.label, not the field_key it will overwrite. 'Remember this answer' defaults to on (line 72), which sends mode auto rather than once.
- `server/routes/onboarding.py:105` — The sibling Profile writer validates keys against known fields. The answer path does not.

### Dummy attacker / affected resource
Untrusted application-form content: any job form that asks a required question about another country's work authorization or sponsorship, or, through the LLM path, one whose benign question gets mapped to an identity key. The owner's own answer is then durably written over a master profile fact the owner did not choose to change.

### Native input and bounded instructions

- `Required Greenhouse select label: "Are you legally authorized to work in the United States? *" (options Yes/No) -> field_key authorized_to_work; owner answer {"authorized_to_work": "No"}`
- `Required Greenhouse select label: "Will you require visa sponsorship to work in the United Kingdom?" -> field_key require_sponsorship; owner answer {"require_sponsorship": "Yes"}`
- Copy the source into the sandbox scratch (work/src, owner-writable, data/ removed). Seed a dummy ProfileDB with location_country=India, authorized_to_work=Yes, require_sponsorship=No, email=owner@example.invalid.
- Build the two controls with GreenhouseAdapter()._field and run standard.resolve against ProfileDB().summary(). Both come back with empty db_value and needs_manual=True, under the keys authorized_to_work and require_sponsorship.
- Add a dummy job with JobsDB(user_id=1).add. Monkeypatch Orchestrator._preflight to return False so nothing reaches a browser. Call Orchestrator(job)._finish_first_pass with those missing_fields, then Orchestrator(job).run_iter2({'authorized_to_work':'No','require_sponsorship':'Yes'}, remember=None), which is the call the /api/apply/answer route makes.
- Read ProfileDB and field_history. Then resolve a new form with 'Are you authorized to work in India?' and 'Do you require visa sponsorship?'. As a control, call _apply_corrections with core.llm.chat_json stubbed to return authorized_to_work, email and phone.
- Run command: sbx.sh <v12 scratch> 120 bash -c 'cd /mnt/work/src && env APP_ENCRYPTION_KEY=<fresh Fernet key> APP_SECRET_KEY=<40-char dummy> python3 /mnt/work/v12_harness.py'. Output was written to scratch work/evidence/v12_answer_key_profile_overwrite.txt, which is predeclared for promotion.

### Observed output and the invariant it proves
V1: both fields have key authorized_to_work / require_sponsorship, db_value '' and needs_manual True. V2: the prompts carry those labels with those keys. V3: the profile changed from authorized_to_work=Yes, require_sponsorship=No to authorized_to_work=No, require_sponsorship=Yes. V4: field_history has ('authorized_to_work','Yes','No','user_prompt') and ('require_sponsorship','No','Yes','user_prompt'). V5: the later India form was auto-filled with 'Are you authorized to work in India?' -> 'No' and 'Do you require visa sponsorship?' -> 'Yes', both needs_manual False. V6: the corrections-path control dropped authorized_to_work and email and set only phone. The LLM-assigned-key variant (for example email) was not run. It follows from the same unfiltered set_many and resolve_missing code.

Invariant violated: An answer given for one application is stored for that application (application_answers) and in the answer bank under the visible question. It reaches the master profile only for allow-listed, application-neutral keys. Identity and compliance keys (authorized_to_work, require_sponsorship, full_name, email, URLs, search_*, cover_letter, resume_summary) change only via the Profile page, as the comment at db/profile_db.py:69-90 states.

### Conditions and containment

- *authentication_level*: The owner answers a missing-field prompt for one of their applications (POST /api/apply/answer requires require_owner).
- *user_interaction*: The owner truthfully answers the question shown (for example, a US or UK work-authorization or sponsorship question) and leaves 'Remember this answer' at its default (on).
- *system_configuration*: A site adapter (Greenhouse or Lever) derives the key deterministically from the label. Or the universal filler is used and the LLM assigns an identity key to a page question.
- *data_state*: The owner's profile holds home-country values (for example location_country=India, authorized_to_work=Yes, require_sponsorship=No).

### Remediation and regression case
Never let a per-application answer rewrite a master profile key unless the key is allow-listed. Write the profile only for keys that are pending prompts of this application and are in CORRECTABLE_FIELDS (or a dedicated ANSWERABLE set). Keep every other answer only in application_answers and the label-keyed answer bank, and pass store=False to resolve_missing for it. Add regression tests: answering a US-authorization prompt keyed authorized_to_work leaves profile.authorized_to_work unchanged, and an LLM-keyed 'email' prompt leaves profile.email unchanged.

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

## Any signed-in user can read the owner's apply session JSON and filled-form screenshots

`server/routes/apply.py:apply-status-and-screenshots-missing-job-ownership` — **medium**

### Ordered trace

1. `server/routes/apply.py:110` (entrypoint, status): GET /api/apply/status/{job_id} takes the job_id from the path and has no uid dependency or ownership check. The only control on the path is the global session gate.
2. `applier/session.py:96` (propagation, ApplicationSession.load): Opens the global cfg.SESSIONS_DIR/<job_id>.json for any id, with no user scoping.
3. `applier/session.py:141` (propagation, ApplicationSession.to_dict): asdict(self) returns the whole session: application_answers, missing_prompts, log and absolute screenshot paths.
4. `tracking/pipeline.py:110` (propagation, run): The only production job writer calls add() on an unscoped JobsDB() (created at line 50), so the row is stored with user_id NULL and later adopted by user 1 (db/jobs_db.py:284).
5. `db/jobs_db.py:386` (propagation, JobsDB._job_id): The job id is sha256(normalized url)[:16] when the writer is unscoped (the pipeline), or sha256('<uid>:<normalized url>')[:16] when it is scoped. Both are deterministic and can be computed from a public posting URL.
6. `server/app.py:107` (sink, create_app /screenshots mount): StaticFiles serves any file in data/screenshots to any signed-in session, including the basename leaked by the status response.

### Evidence

- `server/routes/apply.py:50` — The _get_job(job_id, uid) ownership helper exists and start/answer use it, but status() does not.
- `server/routes/jobs.py:32` — /api/stats filters the global session files to the caller's job ids ('Session files are global; count only the ones for this user's jobs'). This states the intended per-user policy.
- `server/routes/review.py:64` — The review view resolves the job and packet per uid and links /screenshots/<name>, but the mount it links to is unscoped.
- `applier/session.py:307` — Answers the owner types for missing fields are stored in session.application_answers, so the leaked JSON carries real per-application answers.
- `applier/screenshotter.py:16` — Server-side screenshot names are {job_id}_{label}_{HHMMSS}.png. They are low entropy even without the status leak.
- `db/jobs_db.py:284` — Unowned (pipeline-created) rows are adopted by user 1 without changing their URL-only job_id.

### Dummy attacker / affected resource
A signed-in non-owner (user id 2) who knows a public job URL the owner (user id 1) applied to

### Native input and bounded instructions

- `job_id = sha256(JobsDB._norm_url(url).encode()).hexdigest()[:16]  (pipeline-created jobs)`
- `job_id = sha256(('1:' + JobsDB._norm_url('https://www.linkedin.com/jobs/view/9876543210/')).encode()).hexdigest()[:16]  (scoped add, as reproduced)`
- `GET /api/apply/status/{job_id}`
- `GET /screenshots/{basename of screenshots[0]}`
- Copy the source into the sandbox scratch. Point cfg.JOBS_DB_PATH, UsersDB and SettingsDB at temp files and set ALLOW_SIGNUP=True, COOKIE_SECURE=False and a dummy APP_SECRET_KEY of 32+ characters. Then build create_app(ui_dir=<nonexistent>) with TestClient, as tests/test_http_auth.py setUp does.
- Sign up an owner (id 1) and a second user (id 2) on separate clients.
- As the trusted harness, run JobsDB(user_id=1).add for a dummy LinkedIn URL. Write a dummy PNG to cfg.SCREENSHOTS_DIR/<job_id>_filled_101500.png. Save an ApplicationSession for the job with that screenshot path, application_answers {'expected salary': 'V04-DUMMY-1234'} and a dummy log line.
- As user 2, compute the id from the URL alone, then GET /api/apply/status/{id} and GET /screenshots/<basename>. As user 2, also GET /api/jobs/{id} and /api/applications/{id}/review. Anonymously, GET both the status endpoint and the screenshot.
- The script is scratch work/scripts/repro.py. Its output is predeclared for promotion at work/evidence/v04_status_screenshot_nonowner.json.

### Observed output and the invariant it proves
The computed id 4736df4f1bef05c6 equals the owner's job id. As user 2, GET /api/apply/status/4736df4f1bef05c6 returned 200 with the owner's session. It contained application_answers {'expected salary': 'V04-DUMMY-1234'}, the dummy log line and the absolute screenshot path. GET /screenshots/4736df4f1bef05c6_filled_101500.png returned 200 with exactly the dummy PNG bytes. As user 2, /api/jobs/{id} and /api/applications/{id}/review both returned 404. As the owner, /api/jobs/{id} returned 200. Anonymous callers got 401 on status and 303 on the screenshot.

Invariant violated: A session or screenshot is served only when JobsDB(user_id=uid).get_by_id(job_id) resolves for the caller, as /api/stats, /api/jobs/{id} and the review routes already require. Otherwise the response is 404.

### Conditions and containment

- *system_configuration*: ALLOW_SIGNUP=true (server/routes/auth.py:44), so a second account can exist.
- *authentication_level*: Any signed-in non-owner session. Anonymous callers get 401 on the API and 303 on /screenshots.
- *data_state*: The owner has an apply session for a job whose public posting URL the attacker knows or guesses.

### Remediation and regression case
Bind every derived copy to the job owner. status() should require a session user and call _get_job(job_id, uid) before loading the session. Replace the /screenshots StaticFiles mount with a route that takes a single basename, reads the job-id prefix (16 hex characters for both the server and runner naming schemes), checks JobsDB(user_id=uid).get_by_id and serves the file only from inside SCREENSHOTS_DIR. Add a regression test that a second user gets 404 on both paths.

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

## Ghostwriter routes let any signed-in non-owner read the owner's job data and profile PII and spend the owner's LLM key

`server/routes/ghostwriter.py:_get_job_and_profile:unscoped-JobsDB-global-ProfileDB-no-owner-check` — **medium**

### Ordered trace

1. `server/routes/ghostwriter.py:122` (entrypoint, get_context): GET /api/ghostwriter/context/{job_id} takes a caller-chosen job_id and declares no identity or owner dependency. Only the global session gate in server/app.py _install_auth_gate applies.
2. `server/routes/ghostwriter.py:28` (propagation, _get_job_and_profile): JobsDB() is built with user_id=None, so get_by_id (db/jobs_db.py:699-703) adds no user_id filter and returns any user's job row.
3. `server/routes/ghostwriter.py:32` (propagation, _get_job_and_profile): ProfileDB().summary() returns the owner's global profile, including the _flat field map, whoever the caller is.
4. `intelligence/ghostwriter.py:39` (propagation, build_context): A second unscoped get_by_id adds the job's brief, tailored summary and skills. Lines 44-66 add the job description and the owner's name, role, location, education, LinkedIn, GitHub and portfolio.
5. `server/routes/ghostwriter.py:129` (sink, get_context): The full context is returned to the caller as JSON. The chat route (line 114, then intelligence/ghostwriter.py:190 LLM.chat) sends the same context as the system prompt on the global provider key. cover_letter (line 78) and interview_prep (line 93) do the same.

### Evidence

- `db/jobs_db.py:155` — _and() returns an empty filter when user_id is None, so an unscoped JobsDB sees every user's rows.
- `server/routes/jobs.py:17` — Sibling GET /api/jobs/{job_id} uses Depends(get_current_user_id) and JobsDB(user_id=uid). In the verifier run it returned 404 to user 2 for the owner's job.
- `server/routes/kits.py:104` — Sibling kits regenerate LLM route uses Depends(require_owner). In the verifier run it returned 403 to user 2.
- `intelligence/prep.py:193` — load_owner_profile gives the global profile only to PROFILE_OWNER_ID. This shows the intended rule for using the profile in LLM context.
- `core/llm.py:66` — chat() uses _settings(), the single global SettingsDB provider configuration and key, so ghostwriter LLM calls spend the owner's credential.
- `server/routes/auth.py:44` — Signup after the first account is allowed only when cfg.ALLOW_SIGNUP is true, so a non-owner account requires that setting.
- `MULTI_USER_STATUS.md:46` — The project notes that the ghostwriter routes still call JobsDB() unscoped.

### Dummy attacker / affected resource
A signed-in non-owner user (id 2) on an instance where sign-ups are open.

### Native input and bounded instructions

- `GET /api/ghostwriter/context/3111c49c93334335 with user 2's session cookie (owner job id = sha256('1:https://jobs.example.test/posting/123')[:16])`
- `GET /api/ghostwriter/context/<user 2's own job id> with user 2's session cookie`
- `POST /api/ghostwriter/chat/3111c49c93334335 {"message":"hi","history":[]} with user 2's session cookie`
- `POST /api/ghostwriter/cover-letter/<user 2's own job id> {} with user 2's session cookie`
- `POST /api/ghostwriter/interview-prep/3111c49c93334335 with user 2's session cookie`
- Copy the source into sandbox scratch. In-process, point cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, UsersDB and SettingsDB at temp files. Set cfg.ALLOW_SIGNUP=True, cfg.COOKIE_SECURE=False and a 40+ character dummy APP_SECRET_KEY, pass a fresh APP_ENCRYPTION_KEY, then call create_app(ui_dir=<missing dir>) and use TestClient.
- Replace core.llm.chat and core.llm.chat_json with local recording stubs so no provider is contacted.
- Sign up owner@example.test (id 1) and user2@example.test (id 2). Set dummy owner profile fields full_name and linkedin_url. Add one dummy job for each user with JobsDB(user_id=...).add, putting a private marker in the owner's job description.
- As user 2, call /api/jobs/<owner job>, /api/kits/<owner job>/regenerate and the ghostwriter payloads. Also make an anonymous call and an owner call to the context route. Record status codes, context markers and stub calls. Verifier script: scratch work/repro.py. Result promoted from work/evidence/ghostwriter_cross_tenant_v06.json.

### Observed output and the invariant it proves
User 2 got 404 from GET /api/jobs/<owner job> and 403 from POST /api/kits/<owner job>/regenerate. GET /api/ghostwriter/context/<owner job> returned 200 with the owner's job title, the private job-description marker, the owner's full_name and the owner's linkedin_url. With user 2's own job id, the context still held the owner's name and LinkedIn. chat on the owner's job, cover-letter on user 2's own job, and interview-prep on the owner's job each returned 200. They made three LLM stub calls whose system prompts held the owner's profile, and the chat and interview-prep calls also held the owner's private job description. An anonymous request returned 401, and the owner's own request returned 200. The recomputed owner job id matched the stored id 3111c49c93334335.

Invariant violated: Ghostwriter should act only on the caller's own job, through JobsDB(user_id=uid). Because it reads the owner's single global profile and spends the owner's global LLM key, it should be owner-only, like kits regenerate (Depends(require_owner)). The alternative is to give non-owners no profile, as intelligence/prep.py load_owner_profile does.

### Conditions and containment

- *system_configuration*: ALLOW_SIGNUP=true, so accounts other than the owner (user id 1) can exist. The default is false (core/config.py:75).
- *authentication_level*: The attacker has a valid session for any non-owner account.
- *data_state*: To read another user's job, the attacker needs that job's id, which is sha256('<uid>:<normalized url>')[:16] and can be computed from a public posting URL. The owner's profile is exposed with any existing job id, including the attacker's own.

### Remediation and regression case
Make all four ghostwriter routes owner-only with Depends(require_owner), because they read the owner's global profile and spend the owner's global LLM key. Scope the job lookup to the caller with JobsDB(user_id=uid). Regression test: user 2 gets 403 or 404 from all four ghostwriter routes, and the owner still gets 200 for the owner's own job.

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

## In --dev-ui mode, a client-supplied X-Forwarded-For passes through the loopback Next proxy and lets anyone bypass the login brute-force throttle

`core/throttle.py:LoginThrottle:dev-ui-proxy-forwards-client-xff-defeats-login-ratelimit` — **medium**

### Ordered trace

1. `frontend/next.config.mjs:31` (entrypoint, config.rewrites): An unauthenticated client sends POST /api/auth/login to the Next dev server (:3000, started by `next dev`) with an X-Forwarded-For value of its choosing. The rewrite sends /api/:path* to http://127.0.0.1:8000 through Next's proxyRequest/httpxy. That path copies all client headers and overrides only x-forwarded-host.
2. `main.py:187` (propagation, start_server): uvicorn.Config(app, host=cfg.APP_HOST, port=cfg.APP_PORT, log_level='warning') leaves proxy_headers=True and forwarded_allow_ips='127.0.0.1,::1' at their defaults. The proxy connects from 127.0.0.1, so ProxyHeadersMiddleware rewrites scope['client'] from X-Forwarded-For.
3. `server/routes/auth.py:58` (propagation, _client_ip): Returns request.client.host, which is now the value the attacker chose. login() passes it to the throttle at line 64; change_password() does the same at line 120.
4. `core/throttle.py:45` (sink, LoginThrottle._keys): Both buckets, 'acct:{ip}|{email}' and 'ip:{ip}', are keyed only on that ip. A different X-Forwarded-For per attempt gives a different key each time, so retry_after never reaches per_account=5 or per_ip=20.

### Evidence

- `core/throttle.py:45` — The throttle keys come only from the ip argument: f'acct:{ip}|{email.strip().lower()}', f'ip:{ip}'.
- `server/routes/auth.py:58` — _client_ip returns request.client.host. ProxyHeadersMiddleware rewrites this from a trusted loopback peer's X-Forwarded-For.
- `server/routes/auth.py:64` — login() calls login_throttle.retry_after(ip, creds.email) before checking the password. record_failure at line 75 uses the same ip.
- `server/routes/auth.py:121` — change_password() applies the same retry_after/record_failure throttle (lines 121 and 129), keyed on _client_ip.
- `main.py:187` — uvicorn.Config is built without proxy_headers or forwarded_allow_ips, so uvicorn 0.54.0's defaults apply: True and '127.0.0.1,::1' (uvicorn/config.py:225, 363, 543-544).
- `frontend/next.config.mjs:31` — The dev rewrite sends /api/:path* to the loopback backend. In Next 16.3.7, proxy-request.js creates an httpxy ProxyServer with only headers {'x-forwarded-host'} and no xfwd. setupOutgoing does e.headers={...r.headers}, so the client's X-Forwarded-For is relayed unchanged.
- `core/throttle.py:21` — The module comment assumes the app sees the proxy's IP unless uvicorn is started with --proxy-headers. In fact uvicorn enables proxy headers by default.

### Dummy attacker / affected resource
An unauthenticated network client brute-forcing or password-spraying the owner's (or any user's) account while the instance runs in --dev-ui mode.

### Native input and bounded instructions

- `POST /api/auth/login HTTP/1.1  Host: 127.0.0.1:3000  Content-Type: application/json  X-Forwarded-For: 203.0.113.1  {"email":"owner@example.test","password":"wrong0"}`
- `POST /api/auth/login HTTP/1.1  Host: 127.0.0.1:3000  Content-Type: application/json  X-Forwarded-For: 203.0.113.2  {"email":"owner@example.test","password":"wrong1"}`
- Inside the OS-enforced sandbox (no external network, nobody uid, 1 GiB AS), from a writable copy of the source with a dummy APP_ENCRYPTION_KEY/APP_SECRET_KEY: build the real FastAPI app with server.app.create_app(), then serve it with exactly main.py's uvicorn.Config(app, host='127.0.0.1', port=8000, log_level='warning') wrapped by core.shutdown.make_server.
- Run a Node 22 front end on 127.0.0.1:3000 that calls the shipped Next 16.3.7 next/dist/server/lib/router-utils/proxy-request.js proxyRequest() for each request, with the destination from next.config.mjs (http://127.0.0.1:8000 + path). This is the same function router-server.js uses for external rewrites. A full `next dev` run was not used because it cannot initialize under the 1 GiB limit, so the Next routing layer before proxyRequest was checked from source instead: no middleware exists and nothing touches x-forwarded-for.
- Create the dummy owner owner@example.test with signup.
- Control: send 7 wrong-password logins through :3000 with no X-Forwarded-For.
- Attack: clear the in-memory throttle, then send 30 wrong-password logins through :3000, each with a different X-Forwarded-For (203.0.113.1-30). Follow with the correct password using X-Forwarded-For: 198.51.100.7.
- Result file: work/evidence/devproxy_xff_throttle.json (scratch, for parent promotion). The hunter's isolated middleware fixture is at agents/h12-devui/artifacts/work/evidence/xff_trust.json.

### Observed output and the invariant it proves
uvicorn 0.54.0, config.proxy_headers=True, forwarded_allow_ips='127.0.0.1,::1'. The control without XFF through the proxy returned [401,401,401,401,401,429,429], so the throttle works when the key is the real peer. With a rotating XFF through the same proxy, all 30 wrong guesses returned 401 with no 429, which is above both per_account=5 and per_ip=20. The correct password afterwards returned 200. The throttle table held keys like 'acct:203.0.113.10|owner@example.test' and no 'ip:127.0.0.1' entry, confirming the client-chosen XFF value was used as the key.

Invariant violated: Failed logins should be counted against an identity the client cannot forge. A forwarded-for value should only be trusted when a trusted ingress sets it after stripping any copy the client sent. The local Next dev proxy does not strip it, so the backend should not trust it.

### Conditions and containment

- *system_configuration*: The server is started with `python main.py --dev-ui`, so the Next dev server proxies /api to the loopback backend. Production mode, with the static export served directly by uvicorn and a non-loopback peer, is not affected by this path.
- *network_routing*: The attacker can send HTTP requests with a chosen X-Forwarded-For to the Next dev server on :3000. `next dev` is started without -H, so it listens on all interfaces by default and is reachable from the LAN or any network that can reach the host. A cross-site browser page cannot set this header in a simple request.
- *authentication_level*: No authentication is needed for POST /api/auth/login. POST /api/auth/change-password uses the same throttle but requires a session.

### Remediation and regression case
Do not let a client-supplied X-Forwarded-For become the throttle identity. The narrowest source fix is to turn off uvicorn proxy-header trust in main.py's uvicorn.Config unless an operator opts in explicitly, and to require them to name the trusted proxy when they do. All --dev-ui traffic then counts against the real peer (127.0.0.1) instead of a value the attacker can rotate. Optionally, also stop the dev proxy from forwarding client X-Forwarded-For, and fix the misleading comment in core/throttle.py.

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

## Non-owner accounts can read and overwrite the owner's global profile, resume.pdf and search preferences

`server/routes/onboarding.py:profile-resume-routes-missing-require-owner` — **medium**

### Ordered trace

1. `server/routes/onboarding.py:81` (entrypoint, update_profile): POST /api/profile accepts a dict from any signed-in user. The handler has no require_owner dependency.
2. `server/app.py:88` (propagation, auth_gate): The gate only resolves a valid session uid and stores it on request.state. It does no owner or authorization check.
3. `server/routes/onboarding.py:97` (propagation, update_profile): ProfileDB() opens the global cfg.PROFILE_DB_PATH, which is not scoped to the caller. Line 119 then writes the caller's values with set_many.
4. `server/routes/onboarding.py:47` (propagation, upload_resume): upload_resume overwrites the global cfg.RESUME_PATH with the caller's bytes. At line 54 it parses the file and re-imports it into the global profile.
5. `applier/session.py:624` (sink, Orchestrator._owner_profile): The owner's applications use profile_db.summary() from the global ProfileDB. Resume uploads fall back to cfg.RESUME_PATH (applier/form_filler.py:96). So values a non-owner wrote are used in the owner's applications.

### Evidence

- `server/routes/onboarding.py:71` — GET /api/profile returns ProfileDB().get_by_category() with no owner check. /api/profile/flat (line 76) returns get_all() the same way.
- `server/routes/onboarding.py:119` — db.set_many writes caller-supplied values into the global profile.
- `server/routes/onboarding.py:135` — POST /api/search-prefs rewrites search_positions and search_locations in the global profile. tracking/pipeline.py:168 reads them for the owner's scraping run.
- `server/routes/onboarding.py:42` — upload_resume has no require_owner dependency and writes the machine-wide resume.pdf.
- `core/auth.py:190` — The require_owner guard exists, and server/routes uses it about 45 times, but none of these onboarding routes use it.
- `server/routes/answer_bank.py:30` — Sibling code refuses the profile to anyone but PROFILE_OWNER_ID, which shows the intended owner-only policy.
- `applier/form_filler.py:96` — A file upload falls back to cfg.RESUME_PATH when no resume variant is chosen, so a replaced resume.pdf is the one attached to the owner's application.
- `core/resume_parser.py:60` — The resume parse calls LLM.chat_json on the configured provider when text is extracted, so a non-owner's upload can spend the instance's LLM key.
- `MULTI_USER_STATUS.md:39` — Lists the global ProfileDB and resume as a known gap that blocks ALLOW_SIGNUP=true. This confirms the condition, not a disproof.

### Dummy attacker / affected resource
A signed-in non-owner (user id 2) on an instance with ALLOW_SIGNUP=true

### Native input and bounded instructions

- `GET /api/profile/flat`
- `POST /api/profile {"email":"attacker-v07@example.com"}`
- `POST /api/search-prefs {"positions":["BOB-V07-ROLE"]}`
- `POST /api/setup/upload-resume multipart file=x.pdf content '%PDF-1.4 BOB-V07-RESUME'`
- Copy the source into the scratch work/src and make it writable. Run work/repro/v07_repro.py in the OS sandbox with dummy APP_ENCRYPTION_KEY and APP_SECRET_KEY values. The script points the settings, jobs, users and profile DBs and RESUME_PATH at a temp dir, sets COOKIE_SECURE=False, stubs LLM.chat_json, and builds create_app() with a nonexistent ui_dir.
- Sign up owner@example.com (uid 1). Confirm that a second signup returns 403 while ALLOW_SIGNUP is false. Set cfg.ALLOW_SIGNUP=True and sign up bob@example.com (uid 2).
- As the owner, POST /api/profile with a dummy email and phone, and write a dummy resume.pdf.
- As bob, send each payload. Then, as the owner, read /api/profile/flat and /api/search-prefs, and read the bytes of resume.pdf. As a control, check that bob gets 403 on the owner-only /api/notifications/settings and that an anonymous client gets 401.
- Results file for promotion: work/evidence/v07_owner_profile_nonowner.json.

### Observed output and the invariant it proves
Bob's GET /api/profile/flat returned 200 with email owner-v07@example.com and phone +10000000777. GET /api/profile and /api/status also returned 200. Bob's POST /api/profile returned 200 (updated 1), and the owner's email then read attacker-v07@example.com. Bob's POST /api/search-prefs returned 200, and the owner's positions became ['BOB-V07-ROLE']. Bob's upload returned 200, and resume.pdf then contained '%PDF-1.4 BOB-V07-RESUME'. Controls: bob got 403 on /api/notifications/settings, and anonymous got 401 on /api/profile/flat. The dummy PDF had no extractable text, so the LLM-spend path was not exercised and rests on source only.

Invariant violated: Only PROFILE_OWNER_ID may read or change the global profile, resume and search preferences. answer_bank._profile_for (server/routes/answer_bank.py:30) and Orchestrator._owner_profile (applier/session.py:624) refuse the profile to anyone else, and the require_owner comment (core/auth.py:176-188) says the same. A non-owner should get 403, or a per-user profile.

### Conditions and containment

- *system_configuration*: ALLOW_SIGNUP=true must be set so that a second account can exist. The default is false, and the second signup was refused with 403 while it was false.
- *authentication_level*: The attacker holds any valid signed-in session other than user id 1. Anonymous requests get 401.

### Remediation and regression case
Add the require_owner dependency to every onboarding route that touches the global profile, resume or search preferences. Long term, scope ProfileDB and the resume by user_id, as MULTI_USER_STATUS.md plans. Add a regression test that a second user gets 403 on each route and that the owner's profile and resume.pdf stay unchanged.

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
