"""Black-box smoke checks: the deployed App answers over its public protocols.

``agent-foundry smoke`` is the release gate after ``deploy`` + ``wait``. It
only talks to the running App (and, for gateway-fronted payloads, the
workspace MCP gateway) with the caller's workspace credentials -- no
framework state, no in-process agent, no project-root environment.

The framework stays payload-agnostic: a payload adapter contributes its
probes through the optional ``smoke_probes(target)`` hook
(:class:`SmokeCapable`). An instance may add questions in
``instances/<name>/smoke.yml``; payloads that ask questions use them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable
from urllib.error import HTTPError

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

SMOKE_FILENAME: Final = "smoke.yml"
_ERROR_BODY_LIMIT: Final = 300


class SmokeConfigError(ValueError):
    """The instance cannot be smoke-tested as configured."""


class SmokeFailure(AssertionError):
    """A probe reached the App but the reply broke the contract."""


class SmokeQuestion(BaseModel):
    """One ``smoke.yml`` question; a bare string means no expected substrings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    # Case-sensitive substrings the answer must contain.
    expect_contains: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, data: Any) -> Any:
        return {"question": data} if isinstance(data, str) else data

    @field_validator("question")
    @classmethod
    def _question_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("questions must be non-empty strings")
        return stripped

    @field_validator("expect_contains")
    @classmethod
    def _expectations_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("expect_contains entries must be non-empty strings")
        return value


@dataclass(frozen=True)
class SmokeTarget:
    """What a probe may use: the running App and the caller's workspace client."""

    ws: Any
    app_url: str
    workspace_host: str
    instance_dir: Path
    questions: tuple[SmokeQuestion, ...] = ()
    timeout_seconds: float = 300


@dataclass(frozen=True)
class SmokeProbe:
    """One named check. ``run`` returns a short detail and raises on failure."""

    name: str
    run: Callable[[], str]


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str


@runtime_checkable
class SmokeCapable(Protocol):
    """Optional payload-adapter hook; kept off ``AppPayloadAdapter`` so existing adapters stay valid."""

    def smoke_probes(self, target: SmokeTarget) -> list[SmokeProbe]: ...


class SmokeFile(BaseModel):
    """``instances/<name>/smoke.yml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    questions: tuple[SmokeQuestion, ...] = ()


def load_smoke_questions(instance_dir: Path) -> tuple[SmokeQuestion, ...]:
    """Extra questions from ``smoke.yml``; none when the file is absent."""
    path = instance_dir / SMOKE_FILENAME
    if not path.is_file():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return SmokeFile.model_validate(raw).questions
    except ValueError as exc:
        raise SmokeConfigError(f"invalid {path}: {exc}") from exc


def probes_for(adapter: object, target: SmokeTarget) -> list[SmokeProbe]:
    """The adapter's probes; an adapter without the hook cannot be smoke-tested."""
    name = getattr(adapter, "name", type(adapter).__name__)
    if not isinstance(adapter, SmokeCapable):
        raise SmokeConfigError(f"payload adapter {name!r} defines no smoke probes")
    probes = adapter.smoke_probes(target)
    if not probes:
        raise SmokeConfigError(f"payload adapter {name!r} returned no smoke probes")
    return probes


def run_probes(probes: Sequence[SmokeProbe]) -> list[ProbeResult]:
    """Run every probe; one failure never skips the rest."""
    return [_run_one(probe) for probe in probes]


def format_result(result: ProbeResult) -> str:
    status = "PASS" if result.ok else "FAIL"
    return f"{status} {result.name}: {result.detail}"


def require_text(text: str, what: str) -> str:
    """Return a one-line preview of *text*, or fail when it is empty."""
    stripped = " ".join(str(text).split())
    if not stripped:
        raise SmokeFailure(f"{what} returned no text")
    return stripped[:80]


def require_contains(text: str, expected: Sequence[str], what: str) -> None:
    """Fail when *text* lacks any of the *expected* substrings."""
    missing = [item for item in expected if item not in text]
    if missing:
        raise SmokeFailure(f"{what} reply lacks expected text {missing}")


def normalize_host(host: str) -> str:
    """``https://<netloc>`` for a host the SDK may store without a scheme."""
    value = host.strip().rstrip("/")
    return value if "://" in value else f"https://{value}"


def _run_one(probe: SmokeProbe) -> ProbeResult:
    try:
        return ProbeResult(probe.name, True, probe.run())
    except Exception as exc:
        return ProbeResult(probe.name, False, _describe(exc))


def _describe(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        body = exc.read().decode("utf-8", errors="replace")[:_ERROR_BODY_LIMIT]
        return f"HTTP {exc.code}: {body or exc.reason}"
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "SMOKE_FILENAME",
    "ProbeResult",
    "SmokeCapable",
    "SmokeConfigError",
    "SmokeFailure",
    "SmokeFile",
    "SmokeProbe",
    "SmokeQuestion",
    "SmokeTarget",
    "format_result",
    "load_smoke_questions",
    "normalize_host",
    "probes_for",
    "require_contains",
    "require_text",
    "run_probes",
]
