# System map - the full read of 2026-10-10

Every file of the app was read for one question: what can each model send, and what does the code do with
it? This map is the result - what each part owns, every guard, check, repair and nudge with a decision, the
duplications, the dead code and the wrong messages - and the design that follows. The principle, the
user's: **a fixed contract with small genuine variations (an id or a name, any case, a copied listing
line); anything else is refused with what is wrong, what is valid and the next call; guards only for
safety, permission and truth.** Code never guesses what the model meant, never dead-ends it, never tells
it something false, never loses data.

## 1. What was read

| Part | Files (lines) | Owns |
|---|---|---|
| Server | app/main.py (108), app/config.py (74), app/api/assistant.py (412) | the app, settings, the HTTP API |
| Turn | app/assistant/main.py (699), context.py (220), checks.py (212), prompts.py (193), registry.py (595), store.py (279) | main's loop, what every call sees, main's honesty checks, the prompts, every tool's schema and handler, the chat store |
| Helpers | app/assistant/workers/ contracts (200), runner (580), evidence (177), checks (162), repairs (140), __init__ (99) | the helpers' contracts, their step loop, guards, result checks, repairs |
| Providers | app/providers/ base (95), llamacpp (424), gemma_calls (265), groq (119), scripted (50) | model calls, streaming, exact counting, Gemma's text-written calls |
| Tools | app/tools/ - 37 modules (9,118) | each tool: one function, one result shape (core.py) |
| Search | app/tools/search_index.py (318), files.py (289), memory.py (76), app/rag/ (714) | FTS5 + embeddings, documents, recall |
| UI | web/ (4,155) | the chat page; card types checked against what the server sends |

## 2. The parts that are right, and stay

- **Workspace and its resolver** (workspace.py): every item by id, exact name in any case, or a copied listing
  line; a wrong kind says what it is and which tool takes it; an unknown name gets the closest ones. The
  genuine variations, and no more.
- **Tool results** (core.py): one shape - status, a compact result for the model, an artifact for the UI;
  ToolError and pydantic errors become invalid_arguments with the field. Every data tool's errors name the
  problem and the fix ("'date' holds dates; use a line chart for values over time").
- **The sandbox and the approvals** (sandbox.py, approval.py): safety and permission - hard, never confirmable.
- **Grounding** (workers/checks.py): every number from a tool result or a run; sources really read or returned.
- **Context** (context.py): exact counts, a sticky cut, the system prompt that never changes - the prompt cache.
- **inspect_data and the user's choices**: it reports and suggests, never changes data; a question only the
  user can answer is asked and its step applied by code (_settle_questions, _guessed).
- **The search index**: keyword search always works; meaning adds to it when the embedder is up.
- **The provider**: retries only before anything streamed, Stop checked on every line, exact token counts.
- **The UI-server contract**: every card type the server sends is drawn; none is drawn that isn't sent.

## 3. Guards, checks, repairs and nudges - each decided

Measured fact behind the main change: llama-server's `tool_choice: "required"` works with Gemma 4, thinking
on or off (a sum went to `calculate`, a fun fact to `finish`). A helper that must call a tool on every step
can only act or finish: the prose path - and every repair that parses prose - is gone at its root.

| Where | What it does | Decision | Why |
|---|---|---|---|
| runner: plain-text answers | a helper's prose read as its result (_from_text, _json_in_text, _without_finish_markup), checked, retried with "finish by calling ..." | **remove** | tool_choice required; the last step offers only the finish tool |
| repairs: _attribute | credits passages as sources when the knowledge helper names none | **remove** | guesses attribution; the check asks for sources |
| repairs: web sources | credits pages read when none are listed; drops cited pages only seen as snippets | **remove** | edits the model's citations; the check names what is wrong |
| repairs: downloads from the answer | moves a file link written in the answer into downloads | **remove -> check** | the check says "put it in downloads" |
| repairs: tool question copied | puts the tool's own question and options on question_for_user | **remove** | the runner already asks inspect_data's questions in place |
| repairs: code_name | renames a kept script to .py | **remove -> refusal** | "the scratch script is Python: keep it as name.py" |
| runner: _adopt | a code file read becomes the scratch script | **remove -> message** | fix_code on an empty scratch says how: run_code, or main gives the file |
| runner: _unsaved | the app keeps the last working script when the helper never saves | **remove** | the finish check asks for save_code; required tools make it callable |
| runner: auto find | read_webpage gets the last search as find | **remove** | changes the model's call; the page's own note says when it was cut |
| runner: _inspect_first | inspects a table nobody inspected; the call runs after when nothing needs cleaning | keep | correctness: computing on summary rows and text numbers |
| evidence: _repeat | a repeat that worked gets its result back; a failed one is stopped, confirmable | keep | serves the model; no dead end |
| evidence: _guessed, SITE_CAP | a choice only the user can make; a seventh page of one site | keep | permission; politeness |
| main: invented numbers, repeated failure | stop with why; confirm runs it (sample data labelled) | keep | honesty, with a way out |
| main: announce / claim / remember nudges | one hidden [App check] a turn | keep, frozen | measured 17 of 18 right on 301 replies; no new phrases |
| main: written-call nudge | a reply that is a tool call as JSON | **merge into gemma_calls** | two parsers for one failure, with different keys |
| provider: gemma_calls.heal | Gemma's own call syntax written as text, made a real call | keep (main only) | a format adapter, measured; helpers no longer need it |
| main: file= (analyze_or_code) | was reinterpreted by the runner | **strict** | a program, or refused with the field that takes it |

## 4. Duplications and inconsistencies

- **File types** are defined in nine places that disagree (`.txt` is a document when uploaded, a table when
  downloaded): files.TABLES, workspace.TABLE_FILES/IMAGE_SUFFIXES/IMAGE_FORMATS, web.TABLE_SUFFIXES,
  file_ops.BINARY/FROM_MARKDOWN, read_file.CODE, rag.extract.KINDS, sandbox.IMAGES, image.FORMATS, api.IMAGES.
  -> **one module, app/tools/filetypes.py**, that every one of them uses. `.txt` is a document everywhere.
- **Editable as text**: the API refuses .docx edits but lets .pptx through; files(update) has its own rule.
  -> one rule in filetypes.
- **Image checks**: generate_image verifies the image, then workspace.add_image verifies it again with its own
  format list. -> add_image alone.
- **A tool call written as JSON**: gemma_calls.heal and checks.written_call, two key sets. -> heal alone.

## 5. Wrong or missing messages

- web.look: "download_file only takes files on GitHub ... use import_table", "leads away from GitHub;
  download_file" - tools that don't exist (it is files(mode='download')).
- web.look on a PDF from an ordinary site: "not a CSV, TSV or Excel file" - no next step (research_web reads
  documents; the user can upload it).
- read_webpage on a data file: "main brings it in as a table (import_table)".
- query_data: "Tables: data_1, data_2" - ids without names; the model can't map "sales" to an id.
- registry: "There is no tool 'x'." - without the tools there are (old chats hold write_code / analyze_data).
- workspace.WRONG_KIND: nothing for an image wanted but a file or a table given.
- fix_code on an empty scratch: says to use run_code, not how to change a stored script.

## 6. Dead code

- table_reader.list_sheets (nothing calls it).
- After section 3: repairs' prose parsing, _attribute, the web repairs, code_name, _adopt, _unsaved,
  checks.written_call and WRITTEN_NUDGE, HELPER plain-text retry (FIX_ANSWER).

## 7. Done (2026-10-10)

Everything in sections 3-6 is done, with one change of mind: generate_image keeps its own image check - its
messages are about the image service ("sent something that is not a valid image"), which add_image's are not;
both now read the formats from filetypes. New: tests/test_tool_contracts.py checks every tool the same way - a
missing field named, an item not there or of another kind refused saying so, and what every model reads (its
prompt, its tools' and fields' descriptions, those errors) naming only tools that exist; every field a model
fills has a description. A tool added is checked with no test of its own.

## 8. The design after this pass

1. **Helpers act or finish.** Every helper step requires a tool call; the last step offers only its finish.
   No prose results, no parsing, no "call finish" nudges.
2. **One contract per tool.** What it does, what it returns, when, when not (and which instead); every field
   described; strict types. Errors: what is wrong, what is valid, the next call.
3. **Guards only for safety, permission and truth**, each with a way out; judgment guards take confirm.
4. **Checks verify; they don't edit.** A result that fails a check gets one chance with the problem named.
5. **One definition per concept**: file types, editability, image validity, written calls.
