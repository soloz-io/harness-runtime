"""Package for the structured_output middleware.

The middleware itself is in `middleware.py`; anything it owns — tools it
provides, helpers only it uses — lives beside it in this folder rather
than at the top of `core/`, where it was indistinguishable from unrelated
modules. This re-export keeps the import path `core.middleware.structured_output`
unchanged, so the move costs no caller an edit.
"""

from core.middleware.structured_output.middleware import (
    StructuredOutputMappingMiddleware,
    build_tool_strategy,
    resolve_structured_output_model,
)

__all__ = [
    "StructuredOutputMappingMiddleware",
    "build_tool_strategy",
    "resolve_structured_output_model",
]
