"""Agent middleware, one folder each.

Each middleware owns a directory holding `middleware.py` plus whatever belongs
only to it — the tools it provides, helpers nothing else calls. Before this they
were flat modules here while their tools sat at the top of `core/`, so the tools
an agent could actually call were spread across two levels with nothing
connecting them: `ask_user` lived next to `executor.py` and `builder.py` and
read as core machinery rather than as one middleware's payload.

Each package re-exports its public names, so `core.middleware.<name>` still
resolves exactly as before and the reorganisation cost no caller an edit.
"""
