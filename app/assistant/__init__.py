"""The assistant around the tools: which model slot sees which tools, the worker sub-agents, and
the context each model call gets.

    registry.py  every tool's input schema (what the model sees) and how a call runs
    workers.py   worker profiles (web, knowledge, data, coder) and the runner that drives them
    context.py   the automatic context (date, workspace, pinned notes) and the rolling window
"""
