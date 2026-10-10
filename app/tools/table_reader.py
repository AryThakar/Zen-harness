"""Read CSV / Excel exports into a raw table of strings.

Handles the junk real exports contain: title lines above the header, unknown
encodings, ; or tab delimiters, blank rows and columns, and rows with too many
fields. Values are kept as strings - typing is the cleaner's job.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from charset_normalizer import from_bytes

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
DELIMITERS = (",", ";", "\t", "|")
HEADER_SCAN_ROWS = 50

_NUMERIC_OR_DATE = re.compile(
    r"^[\s₹$€£¥(+-]*\d[\d,.\s]*%?\)?$|^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
)


class ReadError(ValueError):
    pass


@dataclass
class RawTable:
    df: pd.DataFrame  # normalized column names, string values ('' = empty), index = file row number
    original_names: dict[str, str]  # normalized -> name as written in the file
    header_row: int  # 1-based row number of the header in the file
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    bad_lines: list[tuple[int, list[str]]] = field(default_factory=list)


def normalize_name(name: str) -> str:
    """'Unit Price (₹)' -> 'unit_price', 'Customer ID' -> 'customer_id'."""
    return re.sub(r"[^0-9a-z]+", "_", str(name).lower()).strip("_")


def unique_names(names: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for i, raw in enumerate(names):
        base = normalize_name(raw) or f"column_{i + 1}"
        seen[base] += 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return out


def read_file(path: str | Path, sheet: str | int | None = None) -> RawTable:
    path = Path(path)
    if not path.exists():
        raise ReadError(f"File not found: {path}")
    if path.suffix.lower() in EXCEL_SUFFIXES:
        return _read_excel(path, sheet)
    return _read_csv(path)


def find_header_row(rows: list[list[str]]) -> int:
    """Index of the first row that looks like a header.

    A header has at least 2 cells, none of them numbers or dates, and is about
    as wide as the widest row in the sample (title lines are usually 1-2 cells).
    """
    widths = [sum(1 for c in r if str(c).strip()) for r in rows[:HEADER_SCAN_ROWS]]
    widest = max(widths, default=0)
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [str(c).strip() for c in row if str(c).strip()]
        if (
            len(cells) >= 2
            and len(cells) >= 0.6 * widest
            and not any(_NUMERIC_OR_DATE.match(c) for c in cells)
        ):
            return i
    return 0


def _decode(data: bytes) -> tuple[str, str]:
    """UTF-8, then Windows-1252 (what Excel on Windows writes), then a detector's guess.
    UTF-16 (Excel's "Unicode Text" export) is recognised by its byte-order mark."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    best = from_bytes(data[:200_000]).best()
    encoding = best.encoding if best else "latin-1"
    return data.decode(encoding, errors="replace"), encoding


def _sniff_delimiter(lines: list[str]) -> str:
    """The delimiter that splits the most lines into the same number of fields
    (then the most fields). Counting lines, not just widths, matters for decimal
    commas: in '1,5;2,5;3,5' both ',' and ';' give 4 fields, but only ';' also
    splits the header."""
    best, best_score = ",", (0, 0)
    for delim in DELIMITERS:
        widths = [len(r) for r in csv.reader(lines, delimiter=delim) if len(r) > 1]
        if not widths:
            continue
        width, lines_with_width = Counter(widths).most_common(1)[0]
        if (lines_with_width, width) > best_score:
            best, best_score = delim, (lines_with_width, width)
    return best


def _read_csv(path: Path) -> RawTable:
    text, encoding = _decode(path.read_bytes())
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        raise ReadError("The file is empty.")

    sample = lines[:HEADER_SCAN_ROWS]
    delimiter = _sniff_delimiter(sample)
    sample_rows = [next(csv.reader([line], delimiter=delimiter), []) for line in sample]
    header_idx = find_header_row(sample_rows)

    bad_lines: list[tuple[int, list[str]]] = []
    try:
        df = pd.read_csv(
            io.StringIO(text), sep=delimiter, skiprows=header_idx, header=0, dtype=str,
            keep_default_na=False, skip_blank_lines=False, index_col=False,
        )
        # With blank lines kept, data row i sits on file line header + 2 + i -
        # unless a quoted value spans lines; then count lines the slow way.
        if '"' in text and df.apply(lambda col: col.str.contains("\n", regex=False)).to_numpy().any():
            df.index = _record_lines(text, delimiter, header_idx)[:len(df)]
        else:
            df.index = range(header_idx + 2, header_idx + 2 + len(df))
    except pd.errors.ParserError:
        df, bad_lines = _read_csv_tolerant(lines, delimiter, header_idx)

    return _finish(df, header_idx + 1, bad_lines, encoding=encoding, delimiter=delimiter)


def _record_lines(text: str, delimiter: str, header_idx: int) -> list[int]:
    """File line number on which each data record starts (records may span lines)."""
    lines = text.splitlines(keepends=True)[header_idx:]
    reader = csv.reader(lines, delimiter=delimiter)
    next(reader, None)  # the header
    starts, line = [], reader.line_num
    for _ in reader:
        starts.append(header_idx + line + 1)
        line = reader.line_num
    return starts


def _read_csv_tolerant(
    lines: list[str], delimiter: str, header_idx: int
) -> tuple[pd.DataFrame, list[tuple[int, list[str]]]]:
    """Slow path: keep good rows, set aside rows with extra non-empty fields."""
    reader = csv.reader(lines[header_idx:], delimiter=delimiter)
    header = next(reader)
    width = len(header)
    rows, index, bad = [], [], []
    for line_no, row in enumerate(reader, start=header_idx + 2):
        if len(row) > width and any(c.strip() for c in row[width:]):
            bad.append((line_no, row))
            continue
        rows.append((row + [""] * width)[:width])
        index.append(line_no)
    return pd.DataFrame(rows, columns=header, index=index, dtype=str), bad


def _read_excel(path: Path, sheet: str | int | None) -> RawTable:
    try:
        grid = pd.read_excel(path, sheet_name=sheet or 0, header=None, dtype=str)
    except ImportError as exc:  # .xls needs the optional xlrd package
        raise ReadError("Old .xls files are not supported; save the file as .xlsx or CSV.") from exc
    except ValueError as exc:  # unknown sheet name, unreadable workbook
        raise ReadError(str(exc)) from exc
    grid = grid.fillna("")
    if grid.empty:
        raise ReadError("The sheet is empty.")
    header_idx = find_header_row(grid.head(HEADER_SCAN_ROWS).values.tolist())
    df = grid.iloc[header_idx + 1:].copy()
    df.columns = [str(c) for c in grid.iloc[header_idx]]
    # Excel row numbers are 1-based.
    df.index = range(header_idx + 2, header_idx + 2 + len(df))
    sheet_name = sheet if isinstance(sheet, str) else None
    return _finish(df, header_idx + 1, [], sheet=sheet_name)


def _finish(df: pd.DataFrame, header_row: int, bad_lines, **meta) -> RawTable:
    df = df.fillna("").astype(str).apply(lambda col: col.str.strip())
    # Drop columns with no header and no values, then fully blank rows.
    empty_cols = [c for c in df.columns if df[c].eq("").all()]
    df = df.drop(columns=empty_cols)
    df = df[df.ne("").any(axis=1)]
    if df.shape[1] == 0:
        raise ReadError("No columns with data were found.")

    names = unique_names(list(df.columns))
    original = dict(zip(names, [str(c) for c in df.columns]))
    df.columns = names
    return RawTable(df=df, original_names=original, header_row=header_row,
                    bad_lines=bad_lines, **meta)
