"""Settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("BIZ_DB_PATH", "data/business.duckdb")
    # How to read ambiguous dates like 03/04/2025 when a file gives no hint.
    default_date_order: str = os.environ.get("BIZ_DEFAULT_DATE_ORDER", "dayfirst")


settings = Settings()
