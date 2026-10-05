"""Tool calls a Gemma 4 model wrote into its text instead of the tool-call channel, turned back
into calls.

Gemma 4 calls a tool as <|tool_call>call:NAME{key:<|"|>text<|"|>,n:5}<tool_call|>, which
llama-server parses into tool_calls. Now and then the model writes the call without its opening
marker - call:NAME{...}, or just NAME{...} on a line of its own, often after an answer in prose.
Seen: a worker's submit_result{answer: "...", sources:[<|"|>https://...<|"|>]} arrived as text,
and that raw syntax ended up in the answer the user read. Such a call is taken out of the text and
made a real call.

It also writes them Python-style - NAME(answer="...", code="...", options=None) - which is read
with Python's own parser (keyword arguments with literal values only, nothing is evaluated) - and
as a JSON object on lines of its own, maybe in a ```json fence: {"tool_calls": [{"tool_name": NAME,
"tool_args": {...}}]} or one {"name": NAME, "arguments": {...}} (seen: main announced an image and
wrote the generate_image call as JSON, which reached the user as it was).

Only the names of the tools offered count, and only at the start of a line or after call: or the
marker, so prose that mentions a tool is left alone. Arguments are Gemma's syntax: bare or quoted
keys, strings in <|"|> or JSON quotes, numbers, true/false/null, lists and nested objects. A call
that does not parse (cut off by the length limit) is removed from the text but not run.

Approach (name gate, anchoring, balanced scanning) after Unsloth Studio's tool healing
(studio/backend/core/tool_healing.py), written independently.
"""

from __future__ import annotations

import ast
import json
import re

QUOTE = '<|"|>'  # Gemma's string delimiter
OPEN, CLOSE = "<|tool_call>", "<tool_call|>"
BARE = re.compile(r"[A-Za-z_][\w-]*")
_JSON = json.JSONDecoder()


NAME_KEYS = ("name", "tool_name", "tool")
ARGUMENT_KEYS = ("arguments", "tool_args", "args", "parameters", "input")
JSON_START = re.compile(r"(?m)^[ \t]*(?:```(?:json)?[ \t]*\n[ \t]*)?\{")


def heal(text: str, names: set[str]) -> tuple[str, list[tuple[str, dict]]]:
    """(the text without the calls written in it, those calls as (name, arguments))."""
    if not names or ("{" not in text and "(" not in text):
        return text, []
    text, from_json = _json_calls(text, names)
    rest, calls = _written_calls(text, names)
    return (rest, from_json + calls) if from_json else (rest, calls)


def _json_calls(text: str, names: set[str]) -> tuple[str, list[tuple[str, dict]]]:
    """Calls written as a JSON object on lines of their own (a ```json fence around it comes off too)."""
    calls, kept, pos = [], [], 0
    for m in JSON_START.finditer(text):
        if m.start() < pos:
            continue
        try:
            value, end = _JSON.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        found = _calls_in(value, names)
        if not found:
            continue
        fence = re.match(r"\s*```", text[end:])
        kept.append(text[pos:m.start()])
        calls += found
        pos = end + (fence.end() if fence and "```" in text[m.start():m.end()] else 0)
    kept.append(text[pos:])
    return ("".join(kept).strip(), calls) if calls else (text, [])


def _calls_in(value, names: set[str]) -> list[tuple[str, dict]]:
    """The calls in {"tool_calls": [...]} or in one call object - all of offered tools, or none."""
    items = value.get("tool_calls") if isinstance(value, dict) and isinstance(value.get("tool_calls"), list) else [value]
    out = []
    for item in items:
        call = item.get("function") if isinstance(item, dict) and isinstance(item.get("function"), dict) else item
        if not isinstance(call, dict):
            return []
        name = next((call[k] for k in NAME_KEYS if isinstance(call.get(k), str)), None)
        arguments = next((call[k] for k in ARGUMENT_KEYS if k in call), {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return []
        if name not in names or not isinstance(arguments, dict):
            return []
        out.append((name, arguments))
    return out


def _written_calls(text: str, names: set[str]) -> tuple[str, list[tuple[str, dict]]]:
    """Calls in Gemma's own syntax or Python-style, anchored to a line start, call: or the marker."""
    start = re.compile(rf"(?:{re.escape(OPEN)}\s*)?(?:call:\s*)?\b("
                       + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\s*([{(])")
    calls, kept, pos = [], [], 0
    for m in start.finditer(text):
        if m.start() < pos:
            continue
        marked = text[m.start():m.start(1)].strip() != ""  # call: or the opening marker
        line_start = text.rfind("\n", 0, m.start()) + 1
        if not marked and text[line_start:m.start()].strip():
            continue  # mid-sentence: a mention, not a call
        try:
            if m.group(2) == "{":
                parser = _Parser(text, m.end() - 1)
                arguments, end = parser.object(), parser.i
            else:
                arguments, end = _python_call(text, m.end() - 1)
        except (ValueError, IndexError, SyntaxError):
            kept.append(text[pos:m.start()])
            pos = len(text)  # cut off: the rest is the broken call
            break
        if text.startswith(CLOSE, end):
            end += len(CLOSE)
        kept.append(text[pos:m.start()])
        calls.append((m.group(1), arguments))
        pos = end
    kept.append(text[pos:])
    return ("".join(kept).strip(), calls) if calls or pos else (text, [])


def _python_call(text: str, start: int) -> tuple[dict, int]:
    """(keyword arguments, end) of a Python-style call whose '(' is at ``start``. Quotes (also
    triple quotes) and nested brackets are skipped; the call is read by ast, values by literal_eval."""
    depth, k, quote = 0, start, None
    while k < len(text):
        ch = text[k]
        if quote:
            if ch == "\\":
                k += 2
                continue
            if text.startswith(quote, k):
                k += len(quote)
                quote = None
                continue
        elif text.startswith(('"""', "'''"), k):
            quote = text[k:k + 3]
            k += 3
            continue
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                call = ast.parse("f" + text[start:k + 1], mode="eval").body
                if not isinstance(call, ast.Call) or call.args:
                    raise ValueError("not keyword arguments")
                return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}, k + 1
        k += 1
    raise ValueError("unclosed call")


class _Parser:
    """Gemma's argument syntax, from an opening brace."""

    def __init__(self, text: str, i: int):
        self.s, self.i = text, i

    def _space(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _expect(self, ch: str) -> None:
        self._space()
        if not self.s.startswith(ch, self.i):
            raise ValueError(f"expected {ch!r} at {self.i}")
        self.i += 1

    def object(self) -> dict:
        self._expect("{")
        out: dict = {}
        self._space()
        if self.s.startswith("}", self.i):
            self.i += 1
            return out
        while True:
            key = self._key()
            self._expect(":")
            out[key] = self.value()
            self._space()
            if self.s.startswith(",", self.i):
                self.i += 1
                continue
            self._expect("}")
            return out

    def _list(self) -> list:
        self._expect("[")
        out: list = []
        self._space()
        if self.s.startswith("]", self.i):
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self._space()
            if self.s.startswith(",", self.i):
                self.i += 1
                continue
            self._expect("]")
            return out

    def _loose_string(self) -> str:
        """A double-quoted string read leniently: \\n \\t \\" \\\\ as usual, any other escaped
        character as itself."""
        out, k = [], self.i + 1
        while k < len(self.s):
            ch = self.s[k]
            if ch == "\\" and k + 1 < len(self.s):
                nxt = self.s[k + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                k += 2
                continue
            if ch == '"':
                self.i = k + 1
                return "".join(out)
            out.append(ch)
            k += 1
        raise ValueError("unclosed string")

    def _key(self) -> str:
        self._space()
        if self.s.startswith(QUOTE, self.i) or self.s.startswith('"', self.i):
            return str(self.value())
        m = BARE.match(self.s, self.i)
        if not m:
            raise ValueError(f"no key at {self.i}")
        self.i = m.end()
        return m.group(0)

    def value(self):
        self._space()
        s, i = self.s, self.i
        if s.startswith(QUOTE, i):
            end = s.find(QUOTE, i + len(QUOTE))
            if end < 0:
                raise ValueError("unclosed string")
            self.i = end + len(QUOTE)
            return s[i + len(QUOTE):end]
        if s[i] == '"':
            try:
                value, self.i = _JSON.raw_decode(s, i)
                return value
            except ValueError:
                return self._loose_string()  # escapes JSON does not know, e.g. Q4\_K (markdown)
        if s[i] == "{":
            return self.object()
        if s[i] == "[":
            return self._list()
        j = i
        while j < len(s) and s[j] not in ",}]\n":
            j += 1
        token = s[i:j].strip()
        if not token:
            raise ValueError(f"no value at {i}")
        self.i = j
        try:
            return json.loads(token)  # numbers, true, false, null
        except ValueError:
            return token  # a bare word: Gemma leaves some strings unquoted
