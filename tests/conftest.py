from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.db.connection import Database
from app.ingestion.reader import RawTable, unique_names

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
TODAY = date(2026, 9, 27)


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


@pytest.fixture
def write_file(tmp_path):
    def _write(text: str, name: str = "upload.csv", encoding: str = "utf-8") -> Path:
        path = tmp_path / name
        path.write_bytes(text.encode(encoding))
        return path

    return _write


def make_raw(rows: list[dict]) -> RawTable:
    """A RawTable from dicts of strings, as the reader would produce (row numbers from 2)."""
    df = pd.DataFrame(rows).fillna("").astype(str)
    names = unique_names(list(df.columns))
    original = dict(zip(names, df.columns))
    df.columns = names
    df.index = range(2, 2 + len(df))
    return RawTable(df=df, original_names=original, header_row=1)
