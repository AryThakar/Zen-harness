"""What kind of file a name is - one definition for every part of the app (uploads, downloads, links read, the
file panel, files(), scripts' outputs, the reader, the search index).

    TABLE     read as a table: CSV, TSV, Excel                   uploads, downloads, links read
    DOCUMENT  read as text and indexed for search                 uploads, links read, files(read)
              (PDF, Word, PowerPoint, plain text, Markdown)
    IMAGE     an image item: PNG, JPEG, WebP, GIF                 uploads, generated, drawn by scripts
    CODE      program text, shown with line numbers               files(read), read_file, the programs helpers keep
    BINARY    not text: never written or changed as text          files(write/update), the file panel
    FROM_MARKDOWN  documents built from Markdown by code          files(write/update): .docx, .pptx

Before (2026-10-10) these were nine lists in eight modules that disagreed - a .txt was a document when
uploaded and a table when downloaded; the panel refused to edit a .docx as text but not a .pptx.
"""

from __future__ import annotations

from pathlib import PurePath

TABLE = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls")
DOCUMENT = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".txt": "txt", ".md": "md", ".markdown": "md"}
IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".gif")
IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp"),
                 "GIF": ("gif", "image/gif")}  # PIL's format -> (extension, MIME type)
IMAGE_MIME = {f".{ext}": mime for ext, mime in IMAGE_FORMATS.values()} | {".jpeg": "image/jpeg"}
CODE = (".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1",
        ".sql", ".r", ".html", ".css", ".kt", ".swift", ".scala", ".m", ".pl", ".lua")
FROM_MARKDOWN = (".docx", ".pptx")
BINARY = (".pdf", ".xlsx", ".xlsm", ".xls", ".doc", ".ppt", ".odt", ".ods", ".png", ".jpg", ".jpeg", ".gif",
          ".webp", ".bmp", ".ico", ".zip", ".gz", ".7z", ".rar", ".parquet", ".joblib", ".pkl", ".exe", ".dll", ".bin",
          ".mp3", ".mp4", ".wav")
SCRIPT_TABLES = (".csv", ".parquet")  # what a script writes that becomes a table


def suffix(name: str) -> str:
    return PurePath(str(name).strip()).suffix.lower()


def is_table(name: str) -> bool:
    return suffix(name) in TABLE


def is_document(name: str) -> bool:
    return suffix(name) in DOCUMENT


def is_image(name: str) -> bool:
    return suffix(name) in IMAGE


def is_code(name: str) -> bool:
    return suffix(name) in CODE


def editable_as_text(name: str) -> bool:
    """A file whose text can be written or changed as it is: not binary, and not a document built from Markdown
    (that is written whole, from Markdown, by files())."""
    return suffix(name) not in BINARY and suffix(name) not in FROM_MARKDOWN
