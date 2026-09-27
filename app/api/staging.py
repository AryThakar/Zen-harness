"""Uploaded files wait here between 'preview' and 'confirm'."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ALLOWED_SUFFIXES = {".csv", ".txt", ".tsv", ".xlsx", ".xlsm"}
CHUNK = 1024 * 1024
MAX_AGE_SECONDS = 24 * 3600


class StagingError(ValueError):
    pass


class FileTooLarge(StagingError):
    pass


@dataclass
class StagedFile:
    file_id: str
    path: Path
    filename: str
    sheet: str | None


class Staging:
    def __init__(self, root: str | Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, stream: BinaryIO, filename: str, sheet: str | None = None) -> StagedFile:
        name = Path(filename or "upload.csv").name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
            raise StagingError(f"'{suffix or name}' files are not supported (use {allowed}).")
        self.cleanup()
        file_id = uuid.uuid4().hex
        path = self.root / f"{file_id}{suffix}"
        size = 0
        with path.open("wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > self.max_bytes:
                    out.close()
                    path.unlink(missing_ok=True)
                    raise FileTooLarge(f"File is larger than {self.max_bytes // CHUNK} MB.")
                out.write(chunk)
        self._meta(file_id).write_text(json.dumps({"filename": name, "sheet": sheet}))
        return StagedFile(file_id, path, name, sheet)

    def get(self, file_id: str) -> StagedFile:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise KeyError(file_id)
        matches = [p for p in self.root.glob(f"{file_id}.*") if p.suffix != ".json"]
        meta = self._meta(file_id)
        if not matches or not meta.exists():
            raise KeyError(file_id)
        info = json.loads(meta.read_text())
        return StagedFile(file_id, matches[0], info["filename"], info["sheet"])

    def delete(self, file_id: str) -> None:
        for p in self.root.glob(f"{file_id}.*"):
            p.unlink(missing_ok=True)

    def cleanup(self, max_age: float = MAX_AGE_SECONDS) -> None:
        """Remove files that were previewed but never confirmed."""
        cutoff = time.time() - max_age
        for p in self.root.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    def _meta(self, file_id: str) -> Path:
        return self.root / f"{file_id}.json"
