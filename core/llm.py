"""
core/llm.py — Unified LLM client.

Priority: Settings DB (set via UI) → .env fallback → error.
Switching providers: go to Settings page in the UI, no code changes needed.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

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
