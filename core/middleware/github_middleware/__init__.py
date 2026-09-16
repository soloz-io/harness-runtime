"""Package for the github_middleware middleware.

The middleware itself is in `middleware.py`; anything it owns — tools it
provides, helpers only it uses — lives beside it in this folder rather
than at the top of `core/`, where it was indistinguishable from unrelated
modules. This re-export keeps the import path `core.middleware.github_middleware`
unchanged, so the move costs no caller an edit.
"""

from core.middleware.github_middleware.middleware import (
    GitHubMiddleware,
    open_pull_request,
)

__all__ = [
    "GitHubMiddleware",
    "open_pull_request",
]
