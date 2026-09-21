"""Package for the url_fetch_middleware middleware.

The module is named for the tool it provides — `fetch_url.py` holds
`fetch_url` — so the thing an agent calls and the file that defines it carry
one name. `middleware.py` said only that it was middleware, which every file
under `core/middleware/` already says.

Anything the middleware owns — tools it provides, helpers only it uses —
lives beside it in this folder rather than at the top of `core/`, where it
was indistinguishable from unrelated modules. This re-export keeps the import
path `core.middleware.url_fetch_middleware` unchanged, so neither the rename
nor the earlier move costs a caller an edit.
"""

from core.middleware.url_fetch_middleware.fetch_url import (
    UrlFetchMiddleware,
)

__all__ = [
    "UrlFetchMiddleware",
]
