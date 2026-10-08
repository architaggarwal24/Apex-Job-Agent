"""
core/llm.py — Unified LLM client.

Priority: Settings DB (set via UI) → .env fallback → error.
Switching providers: go to Settings page in the UI, no code changes needed.
<<<<<<< HEAD
"""

=======

ASYNC ROUTES: chat()/chat_json() are BLOCKING (synchronous network I/O).
FastAPI route handlers (`async def ...`) must never call them directly —
doing so blocks the entire event loop (all other requests, WebSocket
pings, etc.) for the duration of the LLM call, which can be 60-90s on
some providers. Use achat()/achat_json() from async routes instead —
they run the blocking call in a thread pool with a timeout.
"""

import asyncio
>>>>>>> 4226f10 (.)
import json
import logging
import re

logger = logging.getLogger(__name__)

<<<<<<< HEAD
=======
# Default timeout for achat()/achat_json() — generous enough for slow
# providers (NVIDIA NIM has been observed taking 60-90s/call) while
# still bounding how long an async route can be blocked-on-result.
ASYNC_LLM_TIMEOUT = 90.0

>>>>>>> 4226f10 (.)
_openai_client    = None
_anthropic_client = None
_gemini_model     = None


def _settings() -> dict:
    """Read active provider config from SettingsDB (UI-managed)."""
    try:
        from db.settings_db import SettingsDB
        return SettingsDB.get().llm_config()
    except Exception as e:
        logger.warning(f"[LLM] SettingsDB unavailable ({e}), falling back to .env")
        return _settings_from_env()


def _settings_from_env() -> dict:
    """Fallback: read from .env via core/config.py."""
    from core.config import cfg
    p = cfg.LLM_PROVIDER
    configs = {
        "mistral":    {"type":"openai_compat","api_key":cfg.MISTRAL_API_KEY,   "model":cfg.MISTRAL_MODEL,   "base_url":"https://api.mistral.ai/v1",              "provider":"mistral",    "temperature":cfg.LLM_TEMPERATURE},
        "nvidia":     {"type":"openai_compat","api_key":cfg.NVIDIA_API_KEY,    "model":cfg.NVIDIA_MODEL,    "base_url":cfg.NVIDIA_BASE_URL,                      "provider":"nvidia",     "temperature":cfg.LLM_TEMPERATURE},
        "openrouter": {"type":"openai_compat","api_key":cfg.OPENROUTER_API_KEY,"model":cfg.OPENROUTER_MODEL,"base_url":cfg.OPENROUTER_BASE_URL,                  "provider":"openrouter", "temperature":cfg.LLM_TEMPERATURE},
        "gemini":     {"type":"gemini",       "api_key":cfg.GEMINI_API_KEY,    "model":cfg.GEMINI_MODEL,    "base_url":None,                                     "provider":"gemini",     "temperature":cfg.LLM_TEMPERATURE},
        "ollama":     {"type":"openai_compat","api_key":"ollama",              "model":cfg.OLLAMA_MODEL,    "base_url":cfg.OLLAMA_BASE_URL,                      "provider":"ollama",     "temperature":cfg.LLM_TEMPERATURE},
        "anthropic":  {"type":"anthropic",    "api_key":cfg.ANTHROPIC_API_KEY, "model":cfg.ANTHROPIC_MODEL, "base_url":None,                                     "provider":"anthropic",  "temperature":cfg.LLM_TEMPERATURE},
        "openai":     {"type":"openai_compat","api_key":cfg.OPENAI_API_KEY,    "model":cfg.OPENAI_MODEL,    "base_url":cfg.OPENAI_BASE_URL,                      "provider":"openai",     "temperature":cfg.LLM_TEMPERATURE},
    }
    return configs.get(p, configs["mistral"])


def provider_info() -> dict:
    try:
        from db.settings_db import SettingsDB
        return SettingsDB.get().provider_info()
    except Exception:
        s = _settings_from_env()
        return {"provider": s["provider"], "model": s["model"]}


def chat(messages: list[dict], system: str = "", max_tokens: int = 4096,
         temperature: float | None = None, expect_json: bool = False) -> str:
    s    = _settings()
    temp = temperature if temperature is not None else s.get("temperature", 0.1)

    logger.debug(f"[LLM] {s.get('provider','?')}/{s.get('model','?')} msgs={len(messages)}")

    try:
        t = s.get("type", "openai_compat")
        if t == "anthropic":
            raw = _call_anthropic(messages, system, max_tokens, temp, s)
        elif t == "gemini":
            raw = _call_gemini(messages, system, max_tokens, temp, s)
        else:
            raw = _call_openai(messages, system, max_tokens, temp, s)
    except Exception as e:
        logger.error(f"[LLM] Call failed: {e}")
        raise RuntimeError(f"LLM call failed ({s.get('provider','?')}): {e}") from e

    raw = raw.strip()
    if expect_json:
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
    return raw.strip()


def chat_json(messages: list[dict], system: str = "", max_tokens: int = 4096) -> dict | list:
    raw = chat(messages, system=system, max_tokens=max_tokens,
               temperature=0.0, expect_json=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        retry = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
             "Your response was not valid JSON. Return ONLY the JSON object/array, "
             "no markdown, no explanation."},
        ]
        raw2 = chat(retry, system=system, max_tokens=max_tokens,
                    temperature=0.0, expect_json=True)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError as e:
            logger.error(f"[LLM] Still invalid JSON: {raw2[:200]}")
            raise ValueError(f"LLM returned invalid JSON: {e}") from e


def _call_openai(messages, system, max_tokens, temperature, s) -> str:
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=s["api_key"], base_url=s["base_url"])
    full = ([{"role":"system","content":system}] if system else []) + messages
    r = _openai_client.chat.completions.create(
        model=s["model"], messages=full,
        max_tokens=max_tokens, temperature=temperature)
    return r.choices[0].message.content or ""


def _call_anthropic(messages, system, max_tokens, temperature, s) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=s["api_key"])
    kwargs = dict(model=s["model"], max_tokens=max_tokens, messages=messages,
                  temperature=max(0.0, min(1.0, temperature)))
    if system:
        kwargs["system"] = system
    r = _anthropic_client.messages.create(**kwargs)
    return r.content[0].text


def _call_gemini(messages, system, max_tokens, temperature, s) -> str:
    global _gemini_model
    if _gemini_model is None:
        import google.generativeai as genai
        genai.configure(api_key=s["api_key"])
        _gemini_model = genai.GenerativeModel(s["model"])
    parts = ([f"[System]\n{system}\n\n"] if system else [])
    for m in messages:
        parts.append(f"[{'User' if m['role']=='user' else 'Assistant'}]\n{m['content']}")
    r = _gemini_model.generate_content(
        "\n\n".join(parts),
        generation_config={"max_output_tokens": max_tokens, "temperature": temperature})
    return r.text or ""
<<<<<<< HEAD
=======


# ── Connection testing & model discovery (Settings screen) ──────────────────
#
# These take an explicit config dict (same shape llm_config() returns) and build
# THROWAWAY clients — never the cached ones above — so testing values the user
# hasn't saved yet can't disturb the live provider.

TEST_TIMEOUT = 25.0
LIST_TIMEOUT = 20.0


class LLMTestError(Exception):
    """An error whose message is already safe and useful to show the user."""


def _redact(text: str, key: str) -> str:
    # Some providers echo the key (or a prefix of it) back inside the error text.
    if key and len(key) >= 8:
        text = text.replace(key, "***")
    return text


def friendly_llm_error(exc: Exception, cfg: dict) -> str:
    """Turn an SDK/HTTP exception into a sentence telling the user what to fix.
    Matches on class names and .status_code so it works across the OpenAI,
    Anthropic and Gemini SDKs without importing any of them."""
    name   = type(exc).__name__
    status = getattr(exc, "status_code", None)
    raw    = str(getattr(exc, "message", None) or exc)
    detail = _redact(raw, cfg.get("api_key", ""))[:300]
    base, model = cfg.get("base_url") or "the provider", cfg.get("model") or "(none)"

    if name in ("AuthenticationError", "Unauthenticated") or status == 401:
        return "The provider rejected the API key (401). Check that it's correct, active and for this provider."
    if name == "PermissionDeniedError" or status == 403:
        return f"Access denied (403) — the key may not have permission for model '{model}'. {detail}"
    if name == "NotFoundError" or status == 404:
        return (f"Not found (404). Check the model name '{model}' — use \u201cFetch models\u201d to see what this "
                f"provider offers — and that the base URL ends in /v1 (e.g. https://host/v1).")
    if name == "RateLimitError" or status == 429:
        return f"Rate limited or out of credit (429). {detail}"
    if name in ("APITimeoutError", "Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError") or "timed out" in raw.lower():
        return f"Timed out waiting for {base}. The server may be slow, asleep, or unreachable."
    if name in ("APIConnectionError", "ConnectError", "ConnectionError", "ConnectionRefusedError"):
        return f"Couldn't connect to {base}. Is the server running, and is the URL correct?"
    if name == "BadRequestError" or status == 400:
        return f"The provider rejected the request (400): {detail}"
    if isinstance(status, int) and status >= 500:
        return f"The provider had an internal error ({status}). Try again in a moment. {detail}"
    return detail or name


def _openai_client_for(cfg: dict):
    from openai import OpenAI
    return OpenAI(api_key=cfg["api_key"] or "not-needed", base_url=cfg["base_url"],
                  timeout=TEST_TIMEOUT, max_retries=0)


def test_connection(cfg: dict) -> dict:
    """One tiny round-trip with `cfg`. Returns {"response", "latency_ms", "note"}
    or raises LLMTestError with a fix-it message."""
    import time
    if not cfg.get("model"):
        raise LLMTestError("No model selected for this provider — pick or add one first.")
    if cfg.get("type") != "openai_compat" or cfg.get("provider") != "ollama":
        if not cfg.get("api_key"):
            raise LLMTestError("No API key set for this provider yet.")

    prompt = [{"role": "user", "content": "Reply with exactly: OK"}]
    t0 = time.monotonic()
    try:
        # 256 tokens, not 10: reasoning models spend the budget on hidden
        # "thinking" first and return an EMPTY reply if it's tiny, which reads
        # as a failure when the connection is actually fine.
        if cfg["type"] == "anthropic":
            import anthropic
            c = anthropic.Anthropic(api_key=cfg["api_key"], timeout=TEST_TIMEOUT, max_retries=0)
            r = c.messages.create(model=cfg["model"], max_tokens=256, messages=prompt)
            text = r.content[0].text if r.content else ""
        elif cfg["type"] == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=cfg["api_key"])
            r = genai.GenerativeModel(cfg["model"]).generate_content(
                prompt[0]["content"], generation_config={"max_output_tokens": 256},
                request_options={"timeout": TEST_TIMEOUT})
            text = r.text or ""
        else:
            r = _openai_client_for(cfg).chat.completions.create(
                model=cfg["model"], messages=prompt, max_tokens=256)
            text = r.choices[0].message.content or ""
    except Exception as e:
        raise LLMTestError(friendly_llm_error(e, cfg)) from e
    finally:
        if cfg.get("type") == "gemini":
            reset_clients()        # genai.configure() is process-global — don't leave the test key behind

    text = text.strip()
    note = "" if text else ("Connected, but the model returned an empty reply — common with reasoning "
                            "models on a small token budget. The connection itself works.")
    return {"response": text, "latency_ms": int((time.monotonic() - t0) * 1000), "note": note}


def list_models(cfg: dict) -> list[str]:
    """Model ids the provider itself reports (GET {base_url}/models for
    OpenAI-compatible servers). Raises LLMTestError with a readable message."""
    try:
        if cfg["type"] == "anthropic":
            import anthropic
            c = anthropic.Anthropic(api_key=cfg["api_key"], timeout=LIST_TIMEOUT, max_retries=0)
            if not hasattr(c, "models"):
                raise LLMTestError("Listing models isn't supported by the installed Anthropic SDK — "
                                   "run: pip install -U anthropic")
            ids = [m.id for m in c.models.list(limit=100)]
        elif cfg["type"] == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=cfg["api_key"])
            ids = [m.name.removeprefix("models/") for m in genai.list_models()
                   if "generateContent" in getattr(m, "supported_generation_methods", [])]
        else:
            ids = [m.id for m in _openai_client_for(cfg).models.list()]
    except LLMTestError:
        raise
    except Exception as e:
        raise LLMTestError(friendly_llm_error(e, cfg)) from e
    finally:
        if cfg.get("type") == "gemini":
            reset_clients()
    return sorted({i for i in ids if isinstance(i, str) and i})[:500]


def reset_clients() -> None:
    """Drop every cached client so the next call rebuilds from current settings."""
    global _openai_client, _anthropic_client, _gemini_model
    _openai_client = _anthropic_client = _gemini_model = None


# ── Async wrappers for FastAPI routes ───────────────────────────────────────

async def achat(messages: list[dict], system: str = "", max_tokens: int = 4096,
                temperature: float | None = None, expect_json: bool = False,
                timeout: float = ASYNC_LLM_TIMEOUT) -> str:
    """
    Async wrapper around chat() for use in `async def` FastAPI routes.
    Runs the blocking call in the default executor and bounds it with
    a timeout so a slow/hung provider can't block a route forever.

    Raises asyncio.TimeoutError if the provider doesn't respond within
    `timeout` seconds.
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: chat(messages, system=system, max_tokens=max_tokens,
                        temperature=temperature, expect_json=expect_json)
        ),
        timeout=timeout,
    )


async def achat_json(messages: list[dict], system: str = "", max_tokens: int = 4096,
                     timeout: float = ASYNC_LLM_TIMEOUT) -> dict | list:
    """Async wrapper around chat_json() — see achat()."""
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: chat_json(messages, system=system, max_tokens=max_tokens)
        ),
        timeout=timeout,
    )


async def run_blocking(fn, *args, timeout: float = ASYNC_LLM_TIMEOUT, **kwargs):
    """
    Generic helper for `async def` routes that need to call a synchronous
    function which internally makes blocking LLM calls (e.g. functions in
    intelligence/scorer.py, intelligence/briefer.py, intelligence/ghostwriter.py).

    Runs fn(*args, **kwargs) in the default thread pool executor with a
    timeout, so the FastAPI event loop is never blocked by a slow provider.

    Raises asyncio.TimeoutError on timeout.
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, lambda: fn(*args, **kwargs)),
        timeout=timeout,
    )
>>>>>>> 4226f10 (.)
