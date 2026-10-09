# Code review log (2026-10-09)

Every source file of the app read whole, in the order the app runs: the server and API, the turn loop,
the helpers, the model providers, the store and search, every tool, then the UI (HTML, JavaScript,
CSS). Each finding has an impact (high: a crash, a wrong answer or lost data; medium: a feature that
misbehaves; low: cleanliness), its cause, how it was confirmed, and what was done.

## Coverage

| Part | Files | Lines | Read |
|---|---|---|---|
| Server, API, config | app/main.py, app/config.py, app/api/assistant.py | 500 | ✓ |
| Turn loop and checks | app/assistant/*.py | 2,000 | ✓ |
| Helpers | app/assistant/workers/*.py | 1,140 | ✓ |
| Model providers | app/providers/*.py | 930 | ✓ |
| Search and documents | app/rag/*.py, app/tools/search_index.py | 610 | ✓ |
| Tools | app/tools/*.py (37 files) | 8,200 | ✓ |
| UI | web/index.html, web/js/*.js, web/app.css, web/fonts/fonts.css | 4,100 | ✓ |

## Findings, by impact

| # | Impact | Where | What is wrong (cause) | Confirmed | Status |
|---|---|---|---|---|---|
| 1 | high | assistant/context.py `fit_turns`, main.py `_agent` | A turn that alone outgrows main's slot (several helper results in one turn) crashes with IndexError once older turns are already cut; before that, the prompt is sent over the limit and the server refuses it. Cause: main only drops whole turns - unlike the helpers it never shortens older tool results inside the current turn. | reproduced (IndexError) | fixed: cut_turns says when the current turn alone doesn't fit, and main then shortens its older tool results, as a helper does (test: fails before, passes after) |
| 2 | medium | providers/llamacpp.py `complete` | A stream cut mid-answer (the Kaggle tunnel drops: `http.client.IncompleteRead`) or a garbled chunk (`JSONDecodeError`) is not turned into a ProviderError: main says "Something went wrong", a helper "stopped with an internal error", instead of "the connection to the model dropped - try again". | read (exception types checked) | fixed: a cut stream or garbled line is a ProviderError 'the connection dropped ... try again'; the token counter and the status check survive it too (test with a server that drops mid-chunk: fails before, passes after) |
| 3 | medium | tools/calc.py | A result too large for a float (171!, 2**2000) raises OverflowError in the finite check: "calculate stopped with an internal error" instead of the number. | reproduced | fixed: a whole number past 50 digits is given as 13 significant digits with its digit count (200! = 7.886578673648e+374, 375 digits) |
| 4 | medium | web/index.html, web/js/cards.js | The UI's libraries come from CDNs at floating versions (marked@15, dompurify@3, chart.js@4, katex@0.16, mermaid@11): a new release changes the page with no change here. | read | fixed: pinned to the versions the page loaded (marked 15.0.12, dompurify 3.4.16, chart.js 4.5.1, katex 0.16.47, mermaid 11.17.2); checked in the browser |
| 5 | low | assistant/main.py `send` | A message typed while the "Try again, answer quickly" card waits is taken as the click on it and lost. The UI hides the text box then, so only a direct API call can hit it. | read | fixed: a typed message clears the card and is sent as a new message (test) |
| 6 | low | tools/make_chart.py `_line` | A line chart over a date column with no values ends in an internal error instead of "No rows to draw". | read | fixed: 'x has no values to draw' |
| 7 | low | tools/query_data.py | `save=true` runs the query a second time without the 10-second limit. | read | fixed: the saving run has the same time limit (sql_guard.frame) |
| 8 | low | item names | Saved query results are all named "query result"; cleaning a cleaned table gives "x (cleaned) (cleaned)". The item contract (one naming rule for every kind) is still to do. | read | partly: a saved query is named 'query of <table>', cleaning never adds a second '(cleaned)'; the item contract itself is still to do |
| 9 | low | app/main.py | `slot()` is typed as returning LlamaCppProvider but returns a GroqProvider for groq. | read | fixed |
| 10 | low | api/assistant.py | No Origin check: another site open in the same browser could upload a file (multipart needs no preflight) if it knew a workspace id. | read | fixed: changes are refused unless they come from the app's own page (test; checked in the browser) |
| 11 | low | dead code | util.js: the "model" icon and file mark (the model kind is gone); app.css: unused rules (.mini, .answer-table, .thinking, .t-k ...), a duplicate `.main` rule, an empty "preview controls" section, and a comment saying the HTML view "runs nothing". | read | fixed: removed (each one checked unused first) |
| 12 | low | stale references | providers/__init__.py (legacy/agent/claude.py), sql_guard.py (legacy's copy), rag/embed.py (Ollama, Library.reindex), and docs/TARGET_MAP.md / SYSTEM_MAP.md / ASSISTANT_PLAN.md named in prompts.py, checks.py, vision.py, scratch.py, reports.py. | read | fixed: legacy and embed notes; the doc names point to ARCHITECTURE.md since the docs merge (2026-10-09) |
| 13 | low | tools/sandbox.py | The sandbox folder is never emptied: every script's files stay there across tasks. | read | kept on purpose: the stored items are separate copies, and a later script may open a file by its name |

Checked and not a bug: a pause in the middle of a step leaves later tool calls without results - the
providers replay only the calls that got one (base.answered_ids).
