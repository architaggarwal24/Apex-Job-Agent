"""
db/settings_db.py — Persistent settings store.

Stores LLM provider selection, API keys, model choices,
browser config, and agent behaviour settings.

Keys are stored as-is in SQLite (local DB, no cloud).
The settings page in the UI reads/writes here directly.
All LLM calls read from here first, then fall back to .env.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

logger = logging.getLogger(__name__)

# All provider definitions with their fields
PROVIDERS = {
    "mistral": {
        "label":    "Mistral AI",
        "url":      "https://console.mistral.ai",
        "models":   [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mistral-7b",
        ],
        "key_label": "API Key",
        "key_env":   "MISTRAL_API_KEY",
        "model_env": "MISTRAL_MODEL",
        "base_url":  "https://api.mistral.ai/v1",
    },
    "nvidia": {
        "label":    "NVIDIA NIM",
        "url":      "https://integrate.api.nvidia.com",
        "models":   [
            "deepseek-ai/deepseek-v4-flash",
            "deepseek-ai/deepseek-v4-pro",
            "moonshotai/kimi-k2-instruct",
            "meta/llama-3.1-70b-instruct",
            "mistralai/mistral-large",
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
            "mistralai/mistral-large",
            "google/gemma-4-31b-it:free",
            "qwen/qwen3-coder:free",
            "deepseek/deepseek-chat",
            "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.1-70b-instruct",
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
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
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
            "qwen2.5:7b",
            "llama3.1:8b",
            "llama3.1:70b",
            "mistral:7b",
            "deepseek-r1:7b",
            "phi3:mini",
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
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-6",
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
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
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
    ("llm_provider",     "mistral",  "llm",     "Active Provider"),
    ("llm_temperature",  "0.1",      "llm",     "Temperature"),
    # Per-provider keys & models
    ("mistral_api_key",      "",                       "llm_keys", "Mistral API Key"),
    ("mistral_model",        "mistral-large-latest",   "llm_keys", "Mistral Model"),
    ("nvidia_api_key",       "",                       "llm_keys", "NVIDIA NIM API Key"),
    ("nvidia_model",         "deepseek-ai/deepseek-v4-flash", "llm_keys", "NVIDIA Model"),
    ("nvidia_base_url",      "https://integrate.api.nvidia.com/v1", "llm_keys", "NVIDIA Base URL"),
    ("openrouter_api_key",   "",                       "llm_keys", "OpenRouter API Key"),
    ("openrouter_model",     "mistralai/mistral-large","llm_keys", "OpenRouter Model"),
    ("gemini_api_key",       "",                       "llm_keys", "Gemini API Key"),
    ("gemini_model",         "gemini-2.0-flash",       "llm_keys", "Gemini Model"),
    ("ollama_model",         "qwen2.5:7b",             "llm_keys", "Ollama Model"),
    ("ollama_base_url",      "http://localhost:11434/v1","llm_keys","Ollama Base URL"),
    ("anthropic_api_key",    "",                       "llm_keys", "Anthropic API Key"),
    ("anthropic_model",      "claude-sonnet-4-20250514","llm_keys","Anthropic Model"),
    ("openai_api_key",       "",                       "llm_keys", "OpenAI API Key"),
    ("openai_model",         "gpt-4o",                 "llm_keys", "OpenAI Model"),
    ("openai_base_url",      "https://api.openai.com/v1","llm_keys","OpenAI Base URL"),
    # Browser
    ("browser_headless", "false", "browser", "Headless Mode"),
    ("browser_slow_mo",  "60",    "browser", "Slow Mo (ms)"),
    # Agent
    ("dry_run",                    "false", "agent", "Dry Run Mode"),
    ("daily_apply_limit",          "30",    "agent", "Daily Apply Limit"),
    ("auto_skip_score_threshold",  "30",    "agent", "Auto-Skip Score Threshold"),
    ("min_delay_between_applies",  "45",    "agent", "Min Delay Between Applies (s)"),
    ("max_delay_between_applies",  "120",   "agent", "Max Delay Between Applies (s)"),
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

    def _migrate_from_env(self, conn):
        """One-time: import existing .env API keys into settings DB."""
        import os
        env_map = {
            "MISTRAL_API_KEY":    "mistral_api_key",
            "MISTRAL_MODEL":      "mistral_model",
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
            if val:
                # Only overwrite if DB value is still the default/empty
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (db_key,)
                ).fetchone()
                if row and not row["value"]:
                    conn.execute(
                        "UPDATE settings SET value=?, updated_at=? WHERE key=?",
                        (val, now, db_key)
                    )

    # ── Read ──────────────────────────────────────────────────────

    def read(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def read_all(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def read_by_category(self) -> dict[str, list[dict]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM settings ORDER BY category, key"
            ).fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["category"], []).append({
                "key": r["key"], "value": r["value"],
                "label": r["label"],
            })
        return out

    def llm_config(self) -> dict:
        """Return complete LLM config for the active provider."""
        s        = self.read_all()
        provider = s.get("llm_provider", "mistral").lower().strip()

        configs = {
            "mistral": {
                "type":     "openai_compat",
                "api_key":  s.get("mistral_api_key", ""),
                "model":    s.get("mistral_model", "mistral-large-latest"),
                "base_url": "https://api.mistral.ai/v1",
            },
            "nvidia": {
                "type":     "openai_compat",
                "api_key":  s.get("nvidia_api_key", ""),
                "model":    s.get("nvidia_model", "deepseek-ai/deepseek-v4-flash"),
                "base_url": s.get("nvidia_base_url", "https://integrate.api.nvidia.com/v1"),
            },
            "openrouter": {
                "type":     "openai_compat",
                "api_key":  s.get("openrouter_api_key", ""),
                "model":    s.get("openrouter_model", "mistralai/mistral-large"),
                "base_url": "https://openrouter.ai/api/v1",
            },
            "gemini": {
                "type":     "gemini",
                "api_key":  s.get("gemini_api_key", ""),
                "model":    s.get("gemini_model", "gemini-2.0-flash"),
                "base_url": None,
            },
            "ollama": {
                "type":     "openai_compat",
                "api_key":  "ollama",
                "model":    s.get("ollama_model", "qwen2.5:7b"),
                "base_url": s.get("ollama_base_url", "http://localhost:11434/v1"),
            },
            "anthropic": {
                "type":     "anthropic",
                "api_key":  s.get("anthropic_api_key", ""),
                "model":    s.get("anthropic_model", "claude-sonnet-4-20250514"),
                "base_url": None,
            },
            "openai": {
                "type":     "openai_compat",
                "api_key":  s.get("openai_api_key", ""),
                "model":    s.get("openai_model", "gpt-4o"),
                "base_url": s.get("openai_base_url", "https://api.openai.com/v1"),
            },
        }

        cfg_data = configs.get(provider, configs["mistral"])
        cfg_data["provider"]    = provider
        cfg_data["temperature"] = float(s.get("llm_temperature", "0.1"))
        return cfg_data

    def provider_info(self) -> dict:
        c = self.llm_config()
        return {"provider": c["provider"], "model": c["model"]}

    # ── Write ─────────────────────────────────────────────────────

    def write(self, key: str, value: str):
        now = datetime.now().isoformat()
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
