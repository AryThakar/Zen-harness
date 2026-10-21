# Contributing to Zen harness

Thanks for your interest in Zen harness. This covers setting it up, verifying a change and submitting it.

## Prerequisites

- **Windows 10 or 11** (the sandbox and the Windows app are written for it; most of the code is portable)
- **Python 3.11+** (tested on 3.13)
- For the browser tool and the page checks: **Node.js 22+** and Playwright MCP, driving the installed
  Microsoft Edge (see the README's *Run it*)
- For anything the model does: a llama-server with Qwen 3.6 35B-A3B - on your GPU, or the Kaggle notebook in
  [`kaggle/`](kaggle/) and its bridge

## Setting up

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock -e ".[dev]"
copy .env.example .env
```

## Verifying a change

These run without the model, and are what CI runs:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Tests marked `real_browser` open pages in the headless browser and skip themselves where there is none.

The tests prove the tools, the contracts and the checks with fixed inputs - they say nothing about how the model
uses them. For a change to a prompt, a tool's description, a check or the reply page, also try it on the real
model and describe what you saw in the pull request: what you asked, what Zen did, and how long it took.
[`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md) lists every guard and check with the measured failure behind it, and
the model's known limits.

## How the code is written

- **Tools are plain functions** with direct tests and no model inside; the model reaches them through fixed
  contracts, and a call that breaks one is refused with a clear reason, never repaired.
- **Checks verify, they don't edit**: a failed one names the problem and the model fixes it.
- Each module opens with a docstring explaining its role and why it is built that way; comments explain **why**,
  often with the measured failure that made a rule necessary.
- Prefer a clearer name or structure over a comment that restates the code; named constants over magic numbers.
- What every model reads - its prompt, tool descriptions, the app's messages - names only tools, modes and fields
  it has (`tests/test_tool_contracts.py` holds this).

## Submitting changes

1. Fork the repository and create a branch.
2. Keep commits focused, with messages that say why.
3. Run the tests above, and try the change on the model where it matters.
4. Open a pull request describing the change, its motivation and how you verified it.

## License

Zen harness is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). By contributing, you agree
that your contribution is licensed under those same terms, and that AryThakar may also license it, as part of
Zen harness, under other terms, including for commercial use.
