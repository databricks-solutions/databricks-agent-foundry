"""The tool reply for arguments that fail validation, shared by the agent-kept tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import ValidationError
    from pydantic_core import ErrorDetails


def _validation_problem(error: ErrorDetails) -> str:
    """Where and why, never the rejected input itself."""
    location = ".".join(str(part) for part in error["loc"]) or "arguments"
    return f"{location}: {error['msg']}"


def invalid_arguments(exc: ValidationError) -> str:
    """The tool reply for arguments that fail validation."""
    problems = "; ".join(_validation_problem(error) for error in exc.errors())
    return f"Error: invalid arguments ({problems})."
