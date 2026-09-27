"""Thin HTTP client for the FastAPI backend. The UI never touches the database."""

from __future__ import annotations

import os
from typing import Any

import requests

API_URL = os.environ.get("BIZ_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = 300  # big uploads can take a while to clean


class ApiError(Exception):
    """A request failed; the message is safe to show to the user."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _call(method: str, path: str, **kwargs) -> Any:
    try:
        r = requests.request(method, f"{API_URL}{path}", timeout=TIMEOUT, **kwargs)
    except requests.ConnectionError as exc:
        raise ApiError(f"Can't reach the API at {API_URL}. Is it running? (uvicorn app.main:app)") from exc
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        if isinstance(detail, list):  # FastAPI validation errors
            detail = "; ".join(f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail)
        raise ApiError(str(detail), r.status_code)
    return r.json() if r.content else None


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None and v != ""}


def health() -> dict:
    return _call("GET", "/health")


def preview(filename: str, data: bytes, sheet: str | None = None) -> dict:
    return _call("POST", "/uploads/preview", files={"file": (filename, data)}, data=_clean({"sheet": sheet}))


def check_mapping(mapping: dict) -> dict:
    return _call("POST", "/uploads/check", json=mapping)


def confirm(file_id: str, mapping: dict, save_profile_as: str | None, profile_name: str | None) -> dict:
    body = {"file_id": file_id, "mapping": mapping, "save_profile_as": save_profile_as or None,
            "profile_name": profile_name}
    return _call("POST", "/uploads", json=body)


def load_demo(months: int = 18) -> dict:
    return _call("POST", "/uploads/demo", json={"months": months})


def uploads() -> list[dict]:
    return _call("GET", "/uploads")


def upload_detail(upload_id: int) -> dict:
    return _call("GET", f"/uploads/{upload_id}")


def rejected(upload_id: int, limit: int = 100, offset: int = 0) -> dict:
    return _call("GET", f"/uploads/{upload_id}/rejected", params={"limit": limit, "offset": offset})


def delete_upload(upload_id: int) -> None:
    _call("DELETE", f"/uploads/{upload_id}")


def data_range() -> dict:
    return _call("GET", "/dashboard/range")


def dimensions() -> list[str]:
    return _call("GET", "/dashboard/dimensions")


def summary(start=None, end=None) -> dict:
    return _call("GET", "/dashboard/summary", params=_clean({"start": start, "end": end}))


def trend(start=None, end=None, grain: str = "auto") -> dict:
    return _call("GET", "/dashboard/trend", params=_clean({"start": start, "end": end, "grain": grain}))


def breakdown(by: str, start=None, end=None, limit: int = 10) -> dict:
    return _call("GET", "/dashboard/breakdown", params=_clean({"by": by, "start": start, "end": end, "limit": limit}))


def sales(**params) -> dict:
    return _call("GET", "/sales", params=_clean(params))


def schema() -> list[dict]:
    return _call("GET", "/schema")


def describe_column(column: str, description: str | None) -> dict:
    return _call("PATCH", f"/schema/{column}", json={"description": description})


def profiles() -> list[dict]:
    return _call("GET", "/profiles")


def delete_profile(name: str) -> None:
    _call("DELETE", f"/profiles/{requests.utils.quote(name, safe='')}")


def start_ml(task: str, body: dict | None = None) -> dict:
    return _call("POST", f"/ml/{task}", json=body) if body is not None else _call("POST", f"/ml/{task}")


def ml_run(run_id: int) -> dict:
    return _call("GET", f"/ml/runs/{run_id}")


def ml_result(task: str) -> dict | None:
    """The latest successful result of a task, or None if there is none yet."""
    try:
        return _call("GET", f"/ml/{task}")
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise


def segment_customers(segment: str, limit: int = 50) -> dict:
    return _call("GET", "/ml/segments/customers", params={"segment": segment, "limit": limit})
