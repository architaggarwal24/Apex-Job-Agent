"""
core/config.py — Single source of truth for all configuration.
Every value comes from .env. Nothing is hardcoded anywhere else.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Config:
    # ── Paths ─────────────────────────────────────────────────────
    ROOT_DIR        = ROOT
    DATA_DIR        = ROOT / "data"
    LOGS_DIR        = ROOT / "logs"
    SESSIONS_DIR    = ROOT / "data" / "sessions"
    SCREENSHOTS_DIR = ROOT / "data" / "screenshots"
    COOKIES_DIR     = ROOT / "data" / "cookies"
    RESUME_PATH     = ROOT / "resume.pdf"
    PROFILE_DB_PATH = ROOT / "data" / "profile.db"
    JOBS_DB_PATH    = ROOT / "data" / "jobs.db"

    # ── LLM ───────────────────────────────────────────────────────
    LLM_PROVIDER    = os.getenv("LLM_PROVIDER",    "mistral").lower().strip()
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))

    MISTRAL_API_KEY   = os.getenv("MISTRAL_API_KEY",   "")
    MISTRAL_MODEL     = os.getenv("MISTRAL_MODEL",     "mistral-large-latest")

    NVIDIA_API_KEY    = os.getenv("NVIDIA_NIM_API_KEY", "")
    NVIDIA_MODEL      = os.getenv("NVIDIA_NIM_MODEL",   "deepseek-ai/deepseek-v4-flash")
    NVIDIA_BASE_URL   = os.getenv("NVIDIA_BASE_URL",    "https://integrate.api.nvidia.com/v1")

    OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY",  "")
    OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL",    "mistralai/mistral-large")
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY",  "")
    GEMINI_MODEL      = os.getenv("GEMINI_MODEL",    "gemini-2.0-flash")

    OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL",    "qwen2.5:7b")
    OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL",   "claude-sonnet-4-20250514")

    OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",  "")
    OPENAI_MODEL      = os.getenv("OPENAI_MODEL",    "gpt-4o")
    OPENAI_BASE_URL   = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # ── Browser ───────────────────────────────────────────────────
    OPERA_EXECUTABLE_PATH = os.getenv("OPERA_EXECUTABLE_PATH", "")
    OPERA_USER_DATA_DIR   = os.getenv("OPERA_USER_DATA_DIR",   "")
    BROWSER_HEADLESS      = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    BROWSER_SLOW_MO       = int(os.getenv("BROWSER_SLOW_MO", "60"))

    # ── Credentials ───────────────────────────────────────────────
    LINKEDIN_EMAIL    = os.getenv("LINKEDIN_EMAIL",    "")
    LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")
    INDEED_EMAIL      = os.getenv("INDEED_EMAIL",      "")
    INDEED_PASSWORD   = os.getenv("INDEED_PASSWORD",   "")

    # ── Server ────────────────────────────────────────────────────
    APP_HOST       = os.getenv("APP_HOST",       "0.0.0.0")
    APP_PORT       = int(os.getenv("APP_PORT",   "8000"))
    APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "change_me")

    # ── Agent behaviour ───────────────────────────────────────────
    DRY_RUN                   = os.getenv("DRY_RUN", "false").lower() == "true"
    DAILY_APPLY_LIMIT         = int(os.getenv("DAILY_APPLY_LIMIT",     "30"))
    AUTO_SKIP_SCORE_THRESHOLD = int(os.getenv("AUTO_SKIP_SCORE_THRESHOLD", "30"))
    MIN_DELAY_BETWEEN_APPLIES = int(os.getenv("MIN_DELAY_BETWEEN_APPLIES", "45"))
    MAX_DELAY_BETWEEN_APPLIES = int(os.getenv("MAX_DELAY_BETWEEN_APPLIES", "120"))

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.DATA_DIR, cls.LOGS_DIR, cls.SESSIONS_DIR,
                  cls.SCREENSHOTS_DIR, cls.COOKIES_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def opera_exe(cls) -> str:
        p = cls.OPERA_EXECUTABLE_PATH
        u = os.environ.get("USERNAME", os.environ.get("USER", "user"))
        return p.replace("<USERNAME>", u).replace("~", os.path.expanduser("~"))

    @classmethod
    def opera_data(cls) -> str:
        p = cls.OPERA_USER_DATA_DIR
        u = os.environ.get("USERNAME", os.environ.get("USER", "user"))
        return p.replace("<USERNAME>", u).replace("~", os.path.expanduser("~"))


cfg = Config()
cfg.ensure_dirs()
