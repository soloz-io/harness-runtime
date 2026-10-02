"""Running one turn: the executor, its per-turn state, and the handlers for the
events the agent graph streams while it runs.

    executor.py   runs a compiled graph for a session and streams its events
    state.py      the typed state one turn accumulates
    helpers.py    helpers shared by the executor and the handlers
    types.py      the stream event type and its aliases
    handlers/     one handler per kind of stream event; handlers/tools.py holds
                  the per-tool handlers the tools handlers dispatch to
"""
