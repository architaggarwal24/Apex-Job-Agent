"""
db/settings_db.py — Persistent settings store.

Stores LLM provider selection, API keys, model choices,
browser config, and agent behaviour settings.

Keys are stored as-is in SQLite (local DB, no cloud).
The settings page in the UI reads/writes here directly.
All LLM calls read from here first, then fall back to .env.
"""

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

logger = logging.getLogger(__name__)

CUSTOM_PREFIX      = "custom-"     # provider id of a user-added provider: "custom-<slug>"
MAX_CUSTOM_PROVIDERS = 20
MAX_EXTRA_MODELS     = 100

# The example .env ships values like "your_gemini_key_here". Copying that file
# to .env (as the README says) used to import those literally as "API keys":
# the provider showed a green "configured" dot and every call failed with an
# authentication error. Anything shaped like a placeholder is treated as unset.
_PLACEHOLDER_RE = re.compile(
    r"^(your[_\- ].*|.*[_\-]here|change[_\- ]?me|x{3,}|\.{3,}|<.*>|todo|none|null)$", re.I)

# Model ids seen in the wild: "gpt-5.6-terra", "qwen3:8b", "deepseek-ai/deepseek-v3.2",
# "openai/gpt-oss-120b:free", "accounts/fireworks/models/llama-v3p1-70b", "@cf/meta/llama-3".
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_.:/@+\-]{0,199}$")


# Which settings are SECRETS: encrypted at rest (core/crypto.py), masked by GET /api/settings and
# never readable through /api/settings/raw. Every API key ("..._api_key"), and any key ending in
# _token or _password (the notification channels' bot token and SMTP password).
SECRET_SUFFIXES = ("_token", "_password")


def is_secret_key(key: str) -> bool:
    return "api_key" in key or key.endswith(SECRET_SUFFIXES)


def looks_like_placeholder(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and bool(_PLACEHOLDER_RE.match(v))


def normalize_model_id(model: str) -> str:
    m = (model or "").strip()          # tolerate stray spaces/newline from a paste
    if not _MODEL_ID_RE.fullmatch(m):
        raise ValueError("Model names can only contain letters, numbers and . _ - : / @ +")
    return m


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:32].strip("-")
    return slug or "provider"


# All provider definitions with their fields
#
# Model lists refreshed Sept 2026 against each provider's live docs/pricing
# pages (see PR notes). Confidence varies by provider — Anthropic and the
# OpenRouter Claude slug are ground-truth verified; OpenAI/NVIDIA
# NIM are cross-checked against current pricing/catalog pages; Gemini's
# generation numbering was churning fast in 2026 across sources, so the
# safer, more-corroborated IDs were chosen over bleeding-edge ones; Ollama
# local tags are the least verifiable from web docs (they depend on what's
# in the local registry) — if a pull fails, check https://ollama.com/library
# for the exact current tag.
# The provider used when none is chosen, or the chosen one no longer exists.
DEFAULT_PROVIDER = "nvidia"

PROVIDERS = {
    "nvidia": {
        "label":    "NVIDIA NIM",
        "url":      "https://integrate.api.nvidia.com",
        "models":   [
            "deepseek-ai/deepseek-v3.2",                       # current flagship reasoning
            "deepseek-ai/deepseek-v3.1-terminus",              # strong function calling / agentic
            "mistralai/mistral-large-3-675b-instruct-2512",
            "openai/gpt-oss-120b",                             # OpenAI's open-weight release
            "meta/llama-3.1-70b-instruct",
        ],
        "key_label": "NIM API Key",
        "key_env":   "NVIDIA_NIM_API_KEY",
        "model_env": "NVIDIA_NIM_MODEL",
        "base_url":  "https://integrate.api.nvidia.com/v1",
    },
    "openrouter": {
        "label":    "OpenRouter",
        "url":      "https://openrouter.ai",
        "models":   [
            "anthropic/claude-sonnet-5",
            "deepseek/deepseek-v3.2",
            "openai/gpt-oss-120b:free",
            "google/gemma-4-31b-it:free",
            "qwen/qwen3-coder:free",
            "meta-llama/llama-4-maverick",
        ],
        "key_label": "API Key",
        "key_env":   "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "base_url":  "https://openrouter.ai/api/v1",
    },
    "gemini": {
        "label":    "Google Gemini",
        "url":      "https://aistudio.google.com/app/apikey",
        "models":   [
            "gemini-3-pro-preview",     # flagship reasoning
            "gemini-3-flash-preview",   # current docs-default, fast + cheap
            "gemini-3.1-flash-lite",    # stable (non-preview) since May 2026
            "gemini-2.5-flash",         # still GA — safe baseline, sunsets Oct 16 2026
            "gemini-2.5-flash-lite",
        ],
        "key_label": "API Key",
        "key_env":   "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "base_url":  None,
    },
    "ollama": {
        "label":    "Ollama (Local)",
        "url":      "https://ollama.com",
        "models":   [
            "llama3.3:70b",
            "qwen3:8b",
            "gpt-oss:20b",     # OpenAI's open-weight model, runs locally
            "deepseek-r1:8b",
            "gemma3:12b",
            "phi4:14b",
        ],
        "key_label": None,          # No API key needed
        "key_env":   None,
        "model_env": "OLLAMA_MODEL",
        "base_url":  "http://localhost:11434/v1",
    },
    "anthropic": {
        "label":    "Anthropic",
        "url":      "https://console.anthropic.com",
        "models":   [
            "claude-sonnet-5",
            "claude-opus-5-5",
            "claude-haiku-4-5-20251001",
            "claude-fable-5-1",
        ],
        "key_label": "API Key",
        "key_env":   "ANTHROPIC_API_KEY",
        "model_env": "ANTHROPIC_MODEL",
        "base_url":  None,
    },
    "openai": {
        "label":    "OpenAI",
        "url":      "https://platform.openai.com",
        "models":   [
            "gpt-5.6-sol",     # flagship
            "gpt-5.6-terra",   # balanced quality/cost (~half the cost of Sol)
            "gpt-5.6-luna",    # fastest/cheapest tier
            "gpt-5-mini",
        ],
        "key_label": "API Key",
        "key_env":   "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "base_url":  "https://api.openai.com/v1",
    },
}

# Default settings schema
DEFAULT_SETTINGS = [
    # (key, default_value, category, label)
    # LLM
    ("llm_provider",     DEFAULT_PROVIDER, "llm", "Active Provider"),
    ("llm_temperature",  "0.1",      "llm",     "Temperature"),
    # Per-provider keys & models
    ("nvidia_api_key",       "",                       "llm_keys", "NVIDIA NIM API Key"),
    ("nvidia_model",         "deepseek-ai/deepseek-v3.2", "llm_keys", "NVIDIA Model"),
    ("nvidia_base_url",      "https://integrate.api.nvidia.com/v1", "llm_keys", "NVIDIA Base URL"),
    ("openrouter_api_key",   "",                       "llm_keys", "OpenRouter API Key"),
    ("openrouter_model",     "deepseek/deepseek-v3.2", "llm_keys", "OpenRouter Model"),
    ("gemini_api_key",       "",                       "llm_keys", "Gemini API Key"),
    ("gemini_model",         "gemini-3-flash-preview", "llm_keys", "Gemini Model"),
    ("ollama_model",         "qwen3:8b",               "llm_keys", "Ollama Model"),
    ("ollama_base_url",      "http://localhost:11434/v1","llm_keys","Ollama Base URL"),
    ("anthropic_api_key",    "",                       "llm_keys", "Anthropic API Key"),
    ("anthropic_model",      "claude-sonnet-5",        "llm_keys","Anthropic Model"),
    ("openai_api_key",       "",                       "llm_keys", "OpenAI API Key"),
    ("openai_model",         "gpt-5.6-terra",          "llm_keys", "OpenAI Model"),
    ("openai_base_url",      "https://api.openai.com/v1","llm_keys","OpenAI Base URL"),
    # Browser
    ("browser_headless", "false", "browser", "Headless Mode"),
    ("browser_slow_mo",  "60",    "browser", "Slow Mo (ms)"),
    # Agent
    ("dry_run",                    "false", "agent", "Dry Run Mode"),
    ("quality_first",              "true",  "agent", "Quality First"),
    ("daily_apply_limit",          "30",    "agent", "Daily Apply Limit"),
    ("auto_skip_score_threshold",  "30",    "agent", "Auto-Skip Score Threshold"),
    ("enable_llm_scoring",          "false", "agent", "Enable AI Job Scoring"),
    ("company_blacklist",          "",      "agent", "Company Blacklist"),
    ("min_salary_lpa",             "0",     "agent", "Min Salary (LPA)"),
    ("min_delay_between_applies",  "45",    "agent", "Min Delay Between Applies (s)"),
    ("max_delay_between_applies",  "120",   "agent", "Max Delay Between Applies (s)"),
    ("apply_mode",                 "auto",   "agent", "Apply Mode (auto | assist)"),
    ("executor",                   "server", "agent", "Executor (server | runner | auto)"),
    ("apply_mode_overrides",       '{"indeed_apply":"assist","linkedin_easy_apply":"assist"}', "agent", "Apply Mode per Site Type"),
    ("stale_days_submitted",       "7",    "agent", "Stale After (days): Submitted"),
    ("stale_days_recruiter_screen", "5",   "agent", "Stale After (days): Recruiter Screen"),
    ("stale_days_other",           "7",    "agent", "Stale After (days): Any Other Stage"),
]


class SettingsDB:
    _instance = None

    def __init__(self, path: Path | None = None):
        self.path = path or (cfg.DATA_DIR / "settings.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed()

    @classmethod
    def get(cls) -> "SettingsDB":
        """Singleton — reuse one instance per process."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL DEFAULT '',
                    category   TEXT NOT NULL DEFAULT 'general',
                    label      TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)

    def _seed(self):
        now = datetime.now().isoformat()
        with self._conn() as c:
            for key, default, category, label in DEFAULT_SETTINGS:
                c.execute("""
                    INSERT OR IGNORE INTO settings (key, value, category, label, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (key, default, category, label, now))
            # Migrate from .env if keys exist there but not in DB
            self._migrate_from_env(c)
            self._clear_placeholder_keys(c)
            # Mistral is no longer a provider: drop its two settings so they stop showing in Settings,
            # and if it was the active one, move to the default.
            c.execute("DELETE FROM settings WHERE key IN ('mistral_api_key', 'mistral_model')")
            c.execute("UPDATE settings SET value=? WHERE key='llm_provider' AND value='mistral'", (DEFAULT_PROVIDER,))

    def _clear_placeholder_keys(self, conn):
        """Repair: earlier versions imported .env.example's placeholder text
        ("your_x_key_here") as if it were a real key. Blank any stored key
        that is only a placeholder, so it stops showing as configured."""
        from core.crypto import decrypt
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE '%api_key%' AND value != ''").fetchall()
        for r in rows:
            if looks_like_placeholder(decrypt(r["value"])):
                conn.execute("UPDATE settings SET value='' WHERE key=?", (r["key"],))
                logger.info(f"[Settings] Cleared placeholder value for {r['key']}")

    def _migrate_from_env(self, conn):
        """One-time: import existing .env API keys into settings DB."""
        import os
        env_map = {
            "NVIDIA_NIM_API_KEY": "nvidia_api_key",
            "NVIDIA_NIM_MODEL":   "nvidia_model",
            "OPENROUTER_API_KEY": "openrouter_api_key",
            "OPENROUTER_MODEL":   "openrouter_model",
            "GEMINI_API_KEY":     "gemini_api_key",
            "GEMINI_MODEL":       "gemini_model",
            "OLLAMA_MODEL":       "ollama_model",
            "OLLAMA_BASE_URL":    "ollama_base_url",
            "ANTHROPIC_API_KEY":  "anthropic_api_key",
            "ANTHROPIC_MODEL":    "anthropic_model",
            "OPENAI_API_KEY":     "openai_api_key",
            "OPENAI_MODEL":       "openai_model",
            "LLM_PROVIDER":       "llm_provider",
            "LLM_TEMPERATURE":    "llm_temperature",
        }
        now = datetime.now().isoformat()
        for env_key, db_key in env_map.items():
            val = os.getenv(env_key, "")
            if val and not looks_like_placeholder(val):
                # Only overwrite if DB value is still the default/empty
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (db_key,)
                ).fetchone()
                if row and not row["value"]:
                    stored = val
                    if "api_key" in db_key:
                        from core.crypto import encrypt
                        stored = encrypt(val)
                    conn.execute(
                        "UPDATE settings SET value=?, updated_at=? WHERE key=?",
                        (stored, now, db_key)
                    )

    # ── Read ──────────────────────────────────────────────────────

    @staticmethod
    def _maybe_decrypt(key: str, value: str) -> str:
        """Secrets (is_secret_key: API keys, *_token, *_password) are stored
        encrypted (see write()) — decrypt them transparently on the way out so
        every existing caller (llm_config, the settings page, etc.) keeps seeing
        plaintext without needing to know encryption is happening underneath."""
        if is_secret_key(key):
            from core.crypto import decrypt
            return decrypt(value)
        return value

    def read(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not r:
                return default
            return self._maybe_decrypt(key, r["value"])

    def unreadable_secrets(self) -> list[str]:
        """Names (never values) of stored secrets the current encryption key can't decrypt."""
        from core.crypto import can_decrypt
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings WHERE value LIKE 'enc:%' ORDER BY key").fetchall()
        return [r["key"] for r in rows if is_secret_key(r["key"]) and not can_decrypt(r["value"])]

    def read_all(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: self._maybe_decrypt(r["key"], r["value"]) for r in rows}

    def read_by_category(self) -> dict[str, list[dict]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM settings ORDER BY category, key"
            ).fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["category"], []).append({
                "key": r["key"], "value": self._maybe_decrypt(r["key"], r["value"]),
                "label": r["label"],
            })
        return out

    def llm_config(self, provider: str | None = None) -> dict:
        """Complete LLM config for `provider` (default: the active one). Taking an
        explicit provider is what lets the Settings screen test or list models
        for the card you're looking at without switching the app over to it."""
        s        = self.read_all()
        provider = (provider or s.get("llm_provider", DEFAULT_PROVIDER)).lower().strip()

        configs = {
            "nvidia": {
                "type":     "openai_compat",
                "api_key":  s.get("nvidia_api_key", ""),
                "model":    s.get("nvidia_model", "deepseek-ai/deepseek-v3.2"),
                "base_url": s.get("nvidia_base_url", "https://integrate.api.nvidia.com/v1"),
            },
            "openrouter": {
                "type":     "openai_compat",
                "api_key":  s.get("openrouter_api_key", ""),
                "model":    s.get("openrouter_model", "deepseek/deepseek-v3.2"),
                "base_url": "https://openrouter.ai/api/v1",
            },
            "gemini": {
                "type":     "gemini",
                "api_key":  s.get("gemini_api_key", ""),
                "model":    s.get("gemini_model", "gemini-3-flash-preview"),
                "base_url": None,
            },
            "ollama": {
                "type":     "openai_compat",
                "api_key":  "ollama",
                "model":    s.get("ollama_model", "qwen3:8b"),
                "base_url": s.get("ollama_base_url", "http://localhost:11434/v1"),
            },
            "anthropic": {
                "type":     "anthropic",
                "api_key":  s.get("anthropic_api_key", ""),
                "model":    s.get("anthropic_model", "claude-sonnet-5"),
                "base_url": None,
            },
            "openai": {
                "type":     "openai_compat",
                "api_key":  s.get("openai_api_key", ""),
                "model":    s.get("openai_model", "gpt-5.6-terra"),
                "base_url": s.get("openai_base_url", "https://api.openai.com/v1"),
            },
        }

        temperature = float(s.get("llm_temperature", "0.1"))

        if provider.startswith(CUSTOM_PREFIX):
            entry = next((p for p in self.list_custom_providers() if p["id"] == provider), None)
            if entry is not None:
                slug = provider[len(CUSTOM_PREFIX):]
                key  = s.get(f"custom_{slug}_api_key", "")
                return {
                    "type":     "openai_compat",
                    # The OpenAI SDK refuses an empty key; local servers (LM Studio,
                    # vLLM…) ignore whatever is sent.
                    "api_key":  "" if looks_like_placeholder(key) else (key or "not-needed"),
                    "model":    s.get(f"custom_{slug}_model", ""),
                    "base_url": entry["base_url"],
                    "provider": provider,
                    "label":    entry["label"],
                    "temperature": temperature,
                }
            logger.warning(f"[Settings] Active provider {provider!r} no longer exists — using {DEFAULT_PROVIDER}")
            provider = DEFAULT_PROVIDER

        if provider not in configs:
            logger.warning(f"[Settings] Unknown provider {provider!r} — using {DEFAULT_PROVIDER}")
            provider = DEFAULT_PROVIDER
        cfg_data = dict(configs[provider])
        if looks_like_placeholder(cfg_data.get("api_key", "")):
            cfg_data["api_key"] = ""
        cfg_data["provider"]    = provider
        cfg_data["label"]       = PROVIDERS[provider]["label"]
        cfg_data["temperature"] = temperature
        return cfg_data

    def provider_info(self) -> dict:
        c = self.llm_config()
        return {"provider": c["provider"], "model": c["model"], "label": c.get("label", c["provider"])}

    # ── Custom (OpenAI-compatible) providers ───────────────────────

    def list_custom_providers(self) -> list[dict]:
        try:
            data = json.loads(self.read("custom_providers", "[]") or "[]")
        except (ValueError, TypeError):
            return []
        out = []
        for p in data if isinstance(data, list) else []:
            if (isinstance(p, dict) and str(p.get("id", "")).startswith(CUSTOM_PREFIX)
                    and p.get("base_url") and p.get("label")):
                out.append({"id": p["id"], "label": p["label"], "base_url": p["base_url"]})
        return out

    def _save_custom_list(self, providers: list[dict]):
        self.write("custom_providers", json.dumps(providers))

    def add_custom_provider(self, label: str, base_url: str, api_key: str = "",
                            model: str = "", models: list[str] | None = None) -> dict:
        from core.urlcheck import validate_base_url
        label = re.sub(r"\s+", " ", (label or "")).strip()
        if not 1 <= len(label) <= 40 or re.search(r"[\x00-\x1f<>]", label):
            raise ValueError("Give the provider a name (1–40 characters)")
        base_url  = validate_base_url(base_url)
        existing  = self.list_custom_providers()
        if len(existing) >= MAX_CUSTOM_PROVIDERS:
            raise ValueError(f"You can add at most {MAX_CUSTOM_PROVIDERS} custom providers")

        slug, taken = slugify(label), {p["id"] for p in existing}
        pid, n = CUSTOM_PREFIX + slug, 1
        while pid in taken:
            n += 1
            pid = f"{CUSTOM_PREFIX}{slug}-{n}"
        slug = pid[len(CUSTOM_PREFIX):]

        # Validate everything BEFORE writing anything, so a bad model name
        # can't leave a half-created provider behind.
        model_list = [normalize_model_id(m) for m in (models or []) if str(m).strip()]
        if model:
            model = normalize_model_id(model)
            if model not in model_list:
                model_list.append(model)

        entry = {"id": pid, "label": label, "base_url": base_url}
        self._save_custom_list(existing + [entry])
        if api_key and not looks_like_placeholder(api_key):
            self.write(f"custom_{slug}_api_key", api_key.strip())
        if model:
            self.write(f"custom_{slug}_model", model)
        if model_list:
            self.write(f"extra_models_{pid}", json.dumps(model_list[:MAX_EXTRA_MODELS]))
        self._invalidate_llm_cache()
        return entry

    def update_custom_provider(self, pid: str, label: str | None = None,
                               base_url: str | None = None) -> dict:
        from core.urlcheck import validate_base_url
        providers = self.list_custom_providers()
        entry = next((p for p in providers if p["id"] == pid), None)
        if entry is None:
            raise LookupError("Provider not found")
        if label is not None:
            label = re.sub(r"\s+", " ", label).strip()
            if not 1 <= len(label) <= 40 or re.search(r"[\x00-\x1f<>]", label):
                raise ValueError("Give the provider a name (1–40 characters)")
            entry["label"] = label
        if base_url is not None:
            entry["base_url"] = validate_base_url(base_url)
        self._save_custom_list(providers)
        self._invalidate_llm_cache()
        return entry

    def delete_custom_provider(self, pid: str) -> None:
        providers = self.list_custom_providers()
        if not any(p["id"] == pid for p in providers):
            raise LookupError("Provider not found")
        slug = pid[len(CUSTOM_PREFIX):]
        self._save_custom_list([p for p in providers if p["id"] != pid])
        with self._conn() as c:
            c.execute("DELETE FROM settings WHERE key IN (?, ?, ?)",
                      (f"custom_{slug}_api_key", f"custom_{slug}_model", f"extra_models_{pid}"))
        # If it was the active provider, don't leave the app pointing at nothing.
        if self.read("llm_provider", "") == pid:
            self.write("llm_provider", DEFAULT_PROVIDER)
        self._invalidate_llm_cache()

    # ── User-added model names, for any provider ───────────────────

    def known_provider(self, pid: str) -> bool:
        return pid in PROVIDERS or any(p["id"] == pid for p in self.list_custom_providers())

    def extra_models(self, pid: str) -> list[str]:
        try:
            data = json.loads(self.read(f"extra_models_{pid}", "[]") or "[]")
        except (ValueError, TypeError):
            return []
        return [m for m in data if isinstance(m, str)] if isinstance(data, list) else []

    def add_extra_model(self, pid: str, model: str) -> list[str]:
        if not self.known_provider(pid):
            raise LookupError("Unknown provider")
        model  = normalize_model_id(model)
        models = self.extra_models(pid)
        if model not in models and model not in PROVIDERS.get(pid, {}).get("models", []):
            if len(models) >= MAX_EXTRA_MODELS:
                raise ValueError(f"You can save at most {MAX_EXTRA_MODELS} custom models per provider")
            models.append(model)
            self.write(f"extra_models_{pid}", json.dumps(models))
        return models

    def remove_extra_model(self, pid: str, model: str) -> list[str]:
        if not self.known_provider(pid):
            raise LookupError("Unknown provider")
        models = [m for m in self.extra_models(pid) if m != model]
        self.write(f"extra_models_{pid}", json.dumps(models))
        return models

    # ── Everything the Providers screen needs, in one shape ────────

    def is_configured(self, pid: str) -> bool:
        """Would a call to this provider plausibly work right now?"""
        from core.urlcheck import is_local_url
        s = self.read_all()
        if pid in PROVIDERS:
            if PROVIDERS[pid].get("key_env") is None:          # Ollama: no key needed
                return True
            return bool(s.get(f"{pid}_api_key", "")) and not looks_like_placeholder(s.get(f"{pid}_api_key", ""))
        entry = next((p for p in self.list_custom_providers() if p["id"] == pid), None)
        if entry is None:
            return False
        key = s.get(f"custom_{pid[len(CUSTOM_PREFIX):]}_api_key", "")
        return (bool(key) and not looks_like_placeholder(key)) or is_local_url(entry["base_url"])

    def all_providers(self) -> dict:
        out = {}
        for pid, p in PROVIDERS.items():
            out[pid] = {**p, "custom": False, "extra_models": self.extra_models(pid),
                        "configured": self.is_configured(pid)}
        for p in self.list_custom_providers():
            out[p["id"]] = {
                "label": p["label"], "url": "", "models": [], "key_label": "API Key",
                "base_url": p["base_url"], "custom": True,
                "extra_models": self.extra_models(p["id"]),
                "configured": self.is_configured(p["id"]),
            }
        return out

    def auto_skip_score_threshold(self) -> int:
        """Jobs scoring below this are auto-skipped. UI-configurable —
        previously several call sites read cfg.AUTO_SKIP_SCORE_THRESHOLD
        (.env only) directly, so changing this in Settings had no effect.
        Now the single source of truth for all of them."""
        try:
            return int(self.read("auto_skip_score_threshold", "30"))
        except ValueError:
            return 30

    # ── Write ─────────────────────────────────────────────────────

    def write(self, key: str, value: str):
        now = datetime.now().isoformat()
        if is_secret_key(key) and value:
            from core.crypto import encrypt
            value = encrypt(value)
        with self._conn() as c:
            c.execute("""
                INSERT INTO settings (key, value, category, label, updated_at)
                VALUES (?, ?, 'general', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, key.replace("_", " ").title(), now))
        # Invalidate LLM client cache when provider settings change
        if key in ("llm_provider", "llm_temperature") or "api_key" in key or "model" in key:
            self._invalidate_llm_cache()

    def write_many(self, updates: dict[str, str]):
        for k, v in updates.items():
            self.write(k, v)
        # Single cache invalidation after bulk write
        self._invalidate_llm_cache()

    def _invalidate_llm_cache(self):
        """Reset the LLM client cache so next call picks up new settings."""
        try:
            import core.llm as llm_module
            llm_module._openai_client    = None
            llm_module._anthropic_client = None
            llm_module._gemini_model     = None
        except Exception:
            pass
