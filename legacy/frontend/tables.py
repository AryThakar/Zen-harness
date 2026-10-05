"""Table helpers shared by pages."""

from __future__ import annotations

import pandas as pd

ROW, REASON = "File row", "Why it wasn't loaded"


def rejected_frame(rows: list[dict]) -> pd.DataFrame:
    """Rejected rows: the file row number and reason first, then the raw values.
    A file column with the same name as ours keeps its value under "<name> (file)"."""
    records = []
    for r in rows:
        raw = {(f"{k} (file)" if k in (ROW, REASON) else k): v for k, v in r["raw"].items()}
        records.append({ROW: r["row_number"], REASON: r["reasons"], **raw})
    return pd.DataFrame(records)
