# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The assistant around the tools: the turn loop, the helpers, what each model call sees and the checks.

    main.py      main's turn (LangGraph: agent -> tools -> agent), streamed to the UI; asks and approvals
    workers/     the helpers (web, knowledge, analyze_or_code): contracts, the runner, the evidence and checks
    prompts.py   every role prompt (main's persona first) and every hidden nudge - each rule in one place
    registry.py  every tool's input schema and description (what the model sees) and how a call runs
    checks.py    main's honesty checks: repeated failures, invented numbers, announced / claimed / asked to
                 remember but nothing done
    context.py   what travels on the messages (time, workspace, agent.md) and the rolling window
    store.py     workspaces, chats and their messages (SQLite)
"""
