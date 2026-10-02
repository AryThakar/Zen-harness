"""What every tool returns, and how tool errors become results instead of crashes.

A tool result has two audiences. ``result`` is compact JSON for the model: the numbers it may
quote and nothing it could mistake for more. ``artifact`` is for the UI only (a table, chart,
image) and never goes back into the model's context.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import ValidationError

Status = Literal["ok", "partial", "failed", "invalid_arguments"]


@dataclass
class ToolResult:
    status: Status
    result: dict
    artifact: dict | None = None

    def for_model(self) -> str:
        return json.dumps({"status": self.status, **self.result}, ensure_ascii=False)


class ToolError(Exception):
    """A tool could not do what was asked. ``invalid`` = the arguments were wrong (the model can
    fix them); otherwise the data or a service did not allow it."""

    def __init__(self, message: str, field: str | None = None, invalid: bool = True):
        super().__init__(message)
        self.message, self.field, self.invalid = message, field, invalid


def ok(result: dict, artifact: dict | None = None, partial: bool = False) -> ToolResult:
    return ToolResult("partial" if partial else "ok", jsonable(result), jsonable(artifact))


def tool(fn):
    """Turn ToolError and pydantic validation errors into a failed / invalid_arguments result."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> ToolResult:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            error = {"message": exc.message, **({"field": exc.field} if exc.field else {})}
            return ToolResult("invalid_arguments" if exc.invalid else "failed", {"error": error})
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            return ToolResult("invalid_arguments", {"error": {"message": f"{where}: {first['msg']}", "field": where}})

    return wrapper


def jsonable(v):
    """JSON-safe copy: no NaN/inf, no numpy or pandas types, dates as ISO strings."""
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return None if pd.isna(v) else v.isoformat()[:10] if _is_midnight(v) else v.isoformat()
    if v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if v is None or isinstance(v, (str, int, bool)):
        return v
    return str(v)


def _is_midnight(v) -> bool:
    return not isinstance(v, datetime) or (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0)


def number(x: float, digits: int = 4) -> float:
    """Round for the model: 4 significant decimals, whole numbers when large."""
    if x is None or not math.isfinite(x):
        return x
    return float(round(x)) if abs(x) >= 1e6 else float(np.round(x, digits))
