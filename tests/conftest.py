from pathlib import Path

import pytest


@pytest.fixture
def write_file(tmp_path):
    def _write(text: str, name: str = "upload.csv", encoding: str = "utf-8") -> Path:
        path = tmp_path / name
        path.write_bytes(text.encode(encoding))
        return path

    return _write
