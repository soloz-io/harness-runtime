"""Package for the human_interaction middleware.

The middleware itself is in `middleware.py`; anything it owns — tools it
provides, helpers only it uses — lives beside it in this folder rather
than at the top of `core/`, where it was indistinguishable from unrelated
modules. This re-export keeps the import path `core.middleware.human_interaction`
unchanged, so the move costs no caller an edit.
"""

from core.middleware.human_interaction.middleware import (
    HumanInteractionMiddleware,
)

__all__ = [
    "HumanInteractionMiddleware",
]
