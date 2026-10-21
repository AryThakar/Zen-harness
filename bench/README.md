# Measurements on the real model

The tests in `tests/` prove the tools and contracts with fixed inputs. These scripts measure what the **model** does
with them: they run the real turn - the same prompts, tools, helpers and checks as the app - against a running
llama-server, and score the answers by code. Each script's docstring says what it measures and how it scores; its
reports (`.md` to read, `.json` with every answer) sit beside it.

| What | Where |
|---|---|
| Whole jobs the way a user works: several tools and helpers in a chain, a dev set and a held-out set run once | [`combos.py`](combos.py) → `combos_dev*.md`, `combos_holdout*.md` |
| Each build phase's acceptance (docs/history/REBUILD_PLAN.md, section 5) | [`phase2.py`](phase2.py), [`phase4.py`](phase4.py), [`phase5.py`](phase5.py) and their reports |
| The helpers alone: real data, documents and the live web, every answer checked by code | [`workers/`](workers/) - `tasks.py` holds the tasks and their truths |
| main's own tools, one suite each, from what users asked | [`main/`](main/) - `suites.py`, `tool_probe.py`, and the probe reports |
| Choices made by measuring: search by meaning, web result order, page extraction, two conversations on one server | [`search_models.py`](search_models.py), [`web_rank.py`](web_rank.py), [`web_extract.py`](web_extract.py), [`web_focus.py`](web_focus.py), [`slot_leak.py`](slot_leak.py) |
| The same measurements per model: Gemma 4 26B-A4B and Qwen 3.6 35B-A3B | [`models/`](models/) |

Tuning was done on dev sets; the held-out sets were run once afterwards, so their scores are the honest ones.
Some scripts read data that is not committed - public datasets in `bench/data/`, or the author's own chats - and
say so in their docstring.
