"""Settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("BIZ_DB_PATH", "data/business.duckdb")
    # How to read ambiguous dates like 03/04/2025 when a file gives no hint.
    default_date_order: str = os.environ.get("BIZ_DEFAULT_DATE_ORDER", "dayfirst")
    # Uploaded files wait here between preview and confirm.
    staging_dir: str = os.environ.get("BIZ_STAGING_DIR", "data/staging")
    max_upload_mb: int = int(os.environ.get("BIZ_MAX_UPLOAD_MB", "100"))
    # Where the Streamlit UI finds the API.
    api_url: str = os.environ.get("BIZ_API_URL", "http://localhost:8000")
    # Currency symbol the AI uses when talking about money.
    currency: str = os.environ.get("BIZ_CURRENCY", "₹")
    # The AI assistant's model. "anthropic" uses Claude through the official SDK
    # (needs ANTHROPIC_API_KEY); a local model via Ollama comes next.
    llm_provider: str = os.environ.get("BIZ_LLM_PROVIDER", "anthropic")
    llm_model: str = os.environ.get("BIZ_LLM_MODEL", "claude-opus-5")
    llm_effort: str | None = os.environ.get("BIZ_LLM_EFFORT") or None  # low | medium | high | xhigh | max
    # The app's own endpoint setting, so it never picks up ANTHROPIC_BASE_URL meant for other tools.
    anthropic_base_url: str = os.environ.get("BIZ_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    # Conversation memory (LangGraph checkpoints).
    chat_db_path: str = os.environ.get("BIZ_CHAT_DB_PATH", "data/chat.sqlite")


settings = Settings()
