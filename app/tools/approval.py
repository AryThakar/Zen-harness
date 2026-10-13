"""Actions that wait for the user's permission (the user's rule), all asked through files()
(file_ops.py): importing a data file from a link as a table, downloading one file from GitHub,
deleting a workspace item, and replacing a file the user brought.

The tool only checks the request and describes it - a link's file name, size and site come from its
first bytes; nothing is downloaded or deleted - as a question card with the action's button and
Cancel. The turn pauses there, as for ask_user. When the user answers, carry_out runs the action by
code: the model never does it itself, and any answer but the button (or a plain yes) counts as no.
"""

from __future__ import annotations

from . import web
from .core import ToolError, ToolResult, ok
from .search_index import SearchIndex
from .workspace import Workspace

CANCEL = "Cancel"
YES = {"yes", "y", "ok", "okay", "sure", "allow", "approve", "go ahead", "do it"}
MAX_DELETE = 10


def import_request(url: str, *, http=None, resolve=None) -> ToolResult:
    looked = web.look(url, tables=True, http=http, resolve=resolve)
    return _ask("import_table", looked, f"Import {looked['name']} ({_size(looked['bytes'])}) from {looked['host']} "
                                        "into this workspace as a table?", "Import")


def download_request(url: str, *, http=None, resolve=None) -> ToolResult:
    looked = web.look(url, github=True, http=http, resolve=resolve)
    return _ask("download_file", looked, f"Download {looked['name']} ({_size(looked['bytes'])}) from GitHub "
                                         "into this workspace?", "Download")


def delete_request(ws: Workspace, items: list[str]) -> ToolResult:
    items = list(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))
    if not items:
        raise ToolError("Name the files to delete, e.g. ['old_report.md'].", field="items")
    if len(items) > MAX_DELETE:
        raise ToolError(f"At most {MAX_DELETE} items at a time.", field="items")
    missing = [i for i in items if i not in ws.ids()]
    if missing:
        raise ToolError(f"No file {', '.join(missing)} in this workspace.", field="items")
    named = [ws.describe(i) for i in items]
    return _ask("delete_files", {"items": items, "named": named},
                f"Delete {', '.join(named)} from this workspace? This can't be undone.", "Delete")


def replace_request(ws: Workspace, item: str, content: str) -> ToolResult:
    """Changing a file the user brought (an upload or a download): it is theirs, so they decide (the version
    before is kept)."""
    e = ws.entry(item)
    details = {"item": item, "name": e["name"], "content": content}
    return _ask("replace_file", details,
                f"Update your file {e['name']}? Its current version is kept and can be restored.", "Update")


def carry_out(ws: Workspace, index: SearchIndex | None, approval: dict, options: list[str], answer: str | None, *,
              http=None, resolve=None) -> tuple[dict, dict | None]:
    """(the tool's result for the model, a card for the UI or None) once the user has answered."""
    said = " ".join((answer or "").split())
    allowed = said.lower() == options[0].lower() or said.lower().rstrip(".!") in YES
    if not allowed:
        # Said as what already happened: "did not allow" alone was read as "not allowed yet" - the model never
        # sees the card, and asked the user to click a button they had just declined (measured on the 26B).
        chose = f"answered '{said}'" if said and said != CANCEL else f"chose {CANCEL}"
        return {"status": "ok", "allowed": False, "cancelled": True,
                "note": f"The user was shown the {options[0]} button and {chose}: it is cancelled, and nothing was "
                        "done. Tell them it was cancelled - don't ask again or try another way."}, None
    try:
        if approval["action"] == "import_table":
            done = web.fetch_table(ws, approval, http=http, resolve=resolve)
        elif approval["action"] == "download_file":
            done = web.fetch_file(ws, index, approval, http=http, resolve=resolve)
        elif approval["action"] == "delete_files":
            done = delete(ws, index, approval["items"])
        elif approval["action"] == "replace_file":
            from .file_ops import replace_file  # file_ops asks through this module
            done = replace_file(ws, index, approval["item"], approval["content"])
        else:
            raise ToolError(f"Unknown action {approval['action']!r}.", invalid=False)
    except ToolError as exc:
        return {"status": "failed", "allowed": True, "error": {"message": exc.message}}, None
    return {"status": done.status, "allowed": True, **done.result}, done.artifact


def delete(ws: Workspace, index: SearchIndex | None, items: list[str]) -> ToolResult:
    gone = []
    for item in items:
        if item not in ws.ids():
            continue
        kind = ws.delete(item)["kind"]
        if index is not None:  # its passages: a document's, a note's, a picture's or a table's, and its report's
            index.remove({"note": "note", "image": "image", "table": "table"}.get(kind, "document"), item)
            index.remove("report", item)
        gone.append(item)
    return ok({"deleted": gone, "summary": f"Deleted {', '.join(gone) or 'nothing'}."})


def _ask(action: str, details: dict, question: str, allow: str) -> ToolResult:
    return ok({"waiting": "The user is asked to approve this."},
              {"type": "question", "question": question, "options": [allow, CANCEL], "allow_text": False,
               "allow_skip": False, "approval": {"action": action, **details}})


def _size(n: int | None) -> str:
    if not n:
        return "size not given"
    return f"{n:,} bytes" if n < 1024 else f"{n / 1024:.1f} KB" if n < 2 ** 20 else f"{n / 2 ** 20:.1f} MB"
