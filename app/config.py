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


settings = Settings()
