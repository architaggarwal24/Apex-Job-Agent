# Needs validation — prioritised leads without severity

These were reproduced locally as mechanisms, but whether they matter in your deployment depends on facts the repository does not contain. Do not treat them as confirmed vulnerabilities and do not send audit traffic to a live instance; the deployment checks are things to *look at*, not to *try*.

## Resume text extraction decodes PDF content streams with no limit on decoded size

`db/resumes_db.py:extract_text+core/resume_parser.py:_extract_text:unbounded-pdf-stream-decompression`

Any signed-in user (including a non-owner when ALLOW_SIGNUP=true, and via /api/setup/upload-resume per the access-control finding) can upload a small PDF whose FlateDecode content stream expands enormously. The server decodes the compressed stream with no bound on decoded size or object count. The 5 MB transfer cap (check_pdf) and the 4-page/6000-character caps act only on the compressed transfer and on the final text, after full decoding. A bounded local measurement confirms the amplification: a 510,229-byte PDF (within the 5 MB cap, valid %PDF- signature) drove db.resumes_db.extract_text to a peak RSS of ~536 MiB (about 500 MiB of transient allocation, ~1100x the file size) in 1.5 s, returning empty text with no exception. The single server process's memory and CPU are shared by all users.

**Claimed root cause.** db/resumes_db.extract_text and core/resume_parser._extract_text call pdfplumber/pdfminer, whose FlateDecode path calls zlib.decompress with no max_length (pdfminer/pdftypes.py:328) and lays out each page in full before truncating. No decoded-byte or object-count budget sits between the 5 MB transfer check (check_pdf, len(data) > MAX_BYTES) and the parser, and the broad except Exception swallows MemoryError, so the request returns normally.

### Source trace

1. `server/routes/kits.py:50` (entrypoint, upload_resume): A signed-in user POSTs a multipart PDF to /api/resumes. The only size gate is file.read(MAX_BYTES + 1) at line 56 (5 MB) plus the %PDF- signature check, both on the still-compressed bytes.
2. `db/resumes_db.py:200` (propagation, ResumeStore.add): After check_pdf validates only transfer size and signature (line 190 -> 93), extract_text(bytes(data)) runs in a worker thread (kits.py:58 asyncio.to_thread).
3. `db/resumes_db.py:143` (sink, extract_text): pdfplumber/pdfminer fully decodes up to TEXT_PAGES=4 pages; pdfminer FlateDecode calls zlib.decompress with no max_length, so a highly compressible stream expands unbounded in server memory before the 6000-character cap is applied at line 144.

### Verified evidence

- `db/resumes_db.py:93` — The only size check is len(data) > MAX_BYTES, applied to the compressed upload (MAX_BYTES = 5 MB).
- `db/resumes_db.py:143` — Each of up to 4 pages is decoded and laid out in full (p.extract_text()) before any decoded-size bound; the compressed content stream is fully inflated here. Local measurement: a 510,229-byte PDF reached ~536 MiB peak RSS (~500 MiB transient, ~1100x the file).
- `db/resumes_db.py:144` — The TEXT_CHARS=6000 truncation is applied only after full extraction, so it does not bound decode-time memory.
- `db/resumes_db.py:145` — The broad except Exception swallows MemoryError (a subclass of Exception); extract_text returns '' and the upload answers HTTP 200, hiding the failure. Confirmed locally: the overflow case returns empty text rather than raising.
- `core/resume_parser.py:80` — _extract_text iterates every page with no page, character or decoded-size cap. Reached from POST /api/setup/upload-resume (onboarding.py:54 parse).
- `server/routes/onboarding.py:48` — shutil.copyfileobj copies the whole upload into cfg.RESUME_PATH with no app-level size cap, then parse() runs synchronously on the event loop over all pages.

### Exact blockers

- Whether this is a meaningful denial of service for other users rather than the graceful self-recovery observed depends on the deployed host's RAM, swap, kernel overcommit and OOM-killer target selection, the asyncio.to_thread pool concurrency, and any reverse-proxy request-body cap (the /api/setup/upload-resume path has no app-level cap at all) — none of which are visible in the repository.
- Observed recovery for a single request is graceful: at the 1 GiB address-space limit the MemoryError raised by decompression is swallowed by extract_text's broad except Exception (db/resumes_db.py:145), so the worker thread returns '' and the request answers HTTP 200 with empty stored text. Whether an attacker can instead exhaust host memory enough for the OS to kill the single uvicorn process (APP_HOST 127.0.0.1:8000, one process) and so take down all users cannot be established from source.

### Bounded local plan
Completed in the parent-approved sandbox (1 GiB address-space limit, uid nobody, no network, read-only target copy, scratch-only writes). A benign, highly-compressible single-page PDF of 510,229 bytes (valid %PDF- signature; passes db.resumes_db.check_pdf; well under the 5 MB cap) was fed to the exact repository sink db.resumes_db.extract_text. Observation (first and only, not scaled up): peak RSS rose from 35.4 MiB baseline to 536.1 MiB (growth 501.5 MiB, ~1100x the uploaded file), elapsed 1.5 s, result returned empty text with no exception (MemoryError path confirmed swallowed in an earlier over-limit build). The content stream decompressed to 524,288,044 bytes from a 509,651-byte compressed stream (~1028x). Promoted evidence file: work/evidence/pdf_decompress_measurement.json (688 bytes). Remaining non-destructive local check for an owner: measure a single upload at the 5 MB cap (expected decoded size ~5 GB at this ratio) and a small fixed number of concurrent uploads under the deployment's real memory/thread limits; use strict resource limits and do not run saturation or production load.

### Owner-observed deployment check
The owner checks whether the server process runs under a memory cgroup or ulimit, the host RAM/swap and kernel overcommit/OOM-killer configuration (whether the uvicorn process would be the OOM victim), the asyncio default thread-pool size, and whether a reverse proxy caps request bodies (especially for the uncapped /api/setup/upload-resume path). No test against the live server is needed.

## Runner screenshot endpoint spools unlimited multipart bodies to disk before the runner token or the 3 MB cap is checked

`server/routes/runner.py:screenshot+server/routes/kits.py:upload_resume:multipart-spooled-before-auth-and-size-check`

The session gate deliberately lets /api/runner/<32 hex>/screenshot through (server/static_shell.py:34, :83; server/app.py:80-81). The route declares file: UploadFile = File(...) and label: Form() (server/routes/runner.py:70). In the installed FastAPI 0.162.0, the request handler awaits request.form() for the whole body before it calls solve_dependencies (fastapi/routing.py:455 then :511), so require_runner (runner.py:38-43) only runs after the body is parsed. Starlette 1.7.0's MultiPartParser applies max_part_size (1 MiB) only to non-file fields. File parts go into SpooledTemporaryFile(max_size=1 MiB) (starlette/formparsers.py:233) with no total-size limit, so anything over 1 MiB rolls to a real temp file. Nothing in the repo bounds the request body: no middleware does, and main.py:187 builds uvicorn.Config with no limit_concurrency. An anonymous client can therefore make the server read and write an arbitrarily large body to TMPDIR before it gets a 401. The 3 MB Content-Length check (runner.py:73), file.read(MAX+1) (runner.py:75) and validate_png (applier/runner_hub.py:110) all run later. The verifier reproduced this independently. The same parse-before-check order makes the 5 MB check on /api/resumes (kits.py:54) ineffective as an ingress bound for signed-in users, but that route is behind the session gate and the anonymous control was stopped with 0 bytes read. For calibration, the JSON runner routes (claim/events/result) also read the whole body into memory before require_runner: an anonymous 2 MiB JSON body was fully consumed before the 401. FastAPI closes the spooled files when the request finishes, so disk is held only while requests are in flight. Exhausting it takes many concurrent or slow streaming uploads, and the shared effect depends on deployment facts the repository does not show.

**Claimed root cause.** The size limits sit inside the endpoint bodies (server/routes/runner.py:72-75, server/routes/kits.py:53-56). FastAPI only calls the endpoint after it has parsed the full multipart body, which Starlette spools to disk with no file-size limit, and after it has resolved Depends(require_runner). The runner paths skip the session gate (server/static_shell.py:34 RUNNER_PATH, is_public_path at :83). No middleware or uvicorn setting (main.py:187) bounds request bodies or concurrency. So the effective pre-authentication ingress bound on this route is none.

### Source trace

1. `server/static_shell.py:34` (entrypoint, RUNNER_PATH): /api/runner/<32 hex>/screenshot exactly matches the regex that exempts runner paths from the session gate, so an anonymous client can reach it
2. `server/app.py:80` (propagation, auth_gate): is_public_path is true, so line 81 calls call_next with no session check and no body limit
3. `server/routes/runner.py:70` (propagation, screenshot): File(...)/Form() parameters make FastAPI await request.form() for the whole body before solving Depends(require_runner). Starlette spools file parts to a SpooledTemporaryFile that rolls to disk after 1 MiB, with no file-size limit
4. `server/routes/runner.py:73` (sink, screenshot): The 3 MB Content-Length check, and the bounded read at line 75, can only act after the body has been received and written to temp disk. require_runner (line 38) returns 401 after that work is done

### Verified evidence

- `server/routes/runner.py:38` — require_runner is a dependency, so FastAPI resolves it after it has parsed the form body
- `server/routes/runner.py:73` — The Content-Length-based 413 is evaluated inside the handler, after body parsing
- `applier/runner_hub.py:110` — validate_png enforces MAX_PNG_BYTES (defined at line 59) on data that has already been read, inside the hub call made after authentication
- `server/routes/kits.py:54` — Same pattern on /api/resumes: the 5 MB Content-Length check runs after the multipart body is spooled. This route is session-gated
- `server/static_shell.py:83` — is_public_path returns true for runner paths, so the session gate does not stop the body
- `main.py:187` — uvicorn.Config(app, host, port, log_level) sets no limit_concurrency or body bound. uvicorn itself has no request-body size limit

### Exact blockers

- Whether the deployed reverse proxy, which .env.example recommends putting in front (Caddy/nginx, comment above APP_HOST), caps request body size for /api/runner/*. nginx defaults to 1 MiB (client_max_body_size). Caddy has no default limit.
- Whether the uvicorn port or the proxy is reachable by untrusted clients. APP_HOST defaults to 127.0.0.1 (core/config.py), but the remote runner documented in runner/runner.py (--server https://...) implies a network-exposed server.
- Whether the uvicorn process's TMPDIR shares a filesystem with data/ (SQLite DBs, encrypted resumes, sessions, screenshots), and how large it is. This decides whether temp disk filled by concurrent in-flight uploads breaks other users' writes.

### Bounded local plan
Already done by verifier v09 in the OS-enforced sandbox (no network, scratch-only writes, prlimit). The script /mnt/work/v09_spool.py (scratch) wraps starlette.formparsers.SpooledTemporaryFile to count bytes written and track rollover. It drives create_app() directly over ASGI with a 6 MiB multipart file part streamed in 64 KiB chunks, with no Content-Length, to /api/runner/<32 zeros>/screenshot. Observed: with no Authorization header and with a bogus Bearer token, status 401 after 6,291,586 body bytes were consumed and 6,291,456 bytes were spooled with rolled_to_disk=true. Control: anonymous /api/resumes returned 401 from the session gate with 0 bytes consumed. Calibration: anonymous 2 MiB JSON to /api/runner/claim returned 401 after the full 2,097,171 bytes were read into memory. Output for promotion: work/evidence/v09_spool.json (the parent promotes it to agents/v09/artifacts/work/evidence/v09_spool.json). Hunter evidence agents/h03-files/artifacts/work/evidence/h1_spool.json agrees, with 8 MiB chunked and declared Content-Length cases.

### Owner-observed deployment check
The owner inspects, without sending traffic to the live deployment: (1) the ingress configuration in front of uvicorn, for a request-body limit that applies to /api/runner/* (nginx client_max_body_size in the relevant server/location block, Caddy request_body { max_size }), and any concurrency or timeout limits for slow uploads; (2) whether port 8000 or the proxy's listener is reachable from untrusted networks (firewall rules, APP_HOST in the real .env); (3) the uvicorn service's TMPDIR (environment or systemd unit) and `df` for its filesystem compared with the filesystem holding data/. If the proxy enforces a body cap of a few MiB or less on these paths, or the server is only reachable from trusted hosts, the shared impact does not hold and the finding becomes hardening. Moving the size checks before body parsing (an ASGI middleware that rejects a Content-Length over the limit and counts streamed bytes for runner and upload paths, or parsing request.form(max_part_size=...) manually after authentication) fixes it in source.
