"""Typed scenario catalog for acceptance lanes.

Kept out of ``examples/*/manifest.toml`` and
``agent_foundry.init.examples_registry`` so init stays display-only.
"""

from __future__ import annotations

import os
import re
import tomllib
from importlib import resources as _resources
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_foundry.init.examples_registry import list_variants
from agent_foundry.testing.live import is_live_path

Capability = Literal["warehouse", "vector_search", "genie"]
Lane = Literal["canary", "full"]
Kind = Literal["brick", "payload", "example"]

BLANK_VARIANT: Final[str] = "blank"
_DEFAULT_CATALOG: Final[str] = "scenarios.toml"
_ENV_FILTER: Final[str] = "AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS"
# Scaffold verbs and check paths are interpolated into workflow shell steps.
_SCAFFOLD_RE: Final = re.compile(r"[a-z][a-z0-9_]*")
_CHECK_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


class ScenarioCatalogError(ValueError):
    """Invalid catalog file, overlay, or filter."""


class Scenario(BaseModel):
    """One acceptance scenario: which level it tests, what to init, and how hard.

    ``kind`` picks the canary job (brick, payload, or example).
    Brick and payload scenarios init the ``blank`` template and add
    ``scaffolds``. Payload and example canaries gate on ``agent-foundry
    smoke``. A brick scenario lists scaffolds in deploy order and
    ``checks[i]``, a repo-relative ``tests/live`` path run from the
    framework checkout, verifies ``scaffolds[i]`` right after
    ``deploy --only`` that brick. ``fixture`` adds the files under
    ``fixtures/<id>/`` to the instance after the scaffolds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: Kind
    variant_id: str = Field(min_length=1)
    lane: Lane
    timeout_minutes: int = Field(gt=0, le=180)
    requires: tuple[Capability, ...] = ()
    scaffolds: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    fixture: bool = False

    @field_validator("id", "variant_id")
    @classmethod
    def _token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(ch.isspace() for ch in stripped):
            raise ValueError("must be a non-empty token without whitespace")
        return stripped

    @field_validator("scaffolds")
    @classmethod
    def _scaffold_verbs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [verb for verb in value if not _SCAFFOLD_RE.fullmatch(verb)]
        if bad:
            raise ValueError(f"scaffold verbs must be lowercase identifiers: {bad}")
        return value

    @field_validator("checks")
    @classmethod
    def _check_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [path for path in value if not _is_live_check_path(path)]
        if bad:
            raise ValueError(f"checks must be relative paths under a tests/live folder: {bad}")
        return value

    @model_validator(mode="after")
    def _level_shape(self) -> Scenario:
        if self.kind == "example" and self.variant_id == BLANK_VARIANT:
            raise ValueError("example scenarios need an examples/ variant, not 'blank'")
        if self.variant_id == BLANK_VARIANT and not self.scaffolds:
            raise ValueError("a 'blank' scenario needs scaffolds, or it deploys nothing")
        if self.kind != "brick":
            return self
        # The brick canary runs ``pytest <checks>``; no paths would collect every testpath.
        if not self.checks:
            raise ValueError("brick scenarios need at least one tests/live check path")
        if len(self.checks) != len(self.scaffolds):
            raise ValueError("brick scenarios pair checks[i] with scaffolds[i] (deploy order); lengths differ")
        return self


def _is_live_check_path(path: str) -> bool:
    if not _CHECK_RE.fullmatch(path):
        return False
    posix = PurePosixPath(path)
    return ".." not in posix.parts and is_live_path(posix)


def load_scenarios(
    overlay: Path | None = None,
    *,
    require_known_variants: bool = True,
    enabled_ids: list[str] | None = None,
) -> list[Scenario]:
    """Load bundled catalog, optional overlay, then apply the env/id filter.

    *overlay* replaces the bundled list when present (forks commit their
    own catalog without patching the wheel). ``AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS``
    is a comma-separated allow-list applied last.
    """
    raw = _read_catalog(_bundled_catalog_path() if overlay is None else overlay)
    scenarios = [Scenario.model_validate(item) for item in raw]
    if not scenarios:
        raise ScenarioCatalogError("scenario catalog is empty")

    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise ScenarioCatalogError(f"duplicate scenario id {scenario.id!r}")
        seen.add(scenario.id)

    if require_known_variants:
        known = {variant.id for variant in list_variants()} | {BLANK_VARIANT}
        unknown = sorted({s.variant_id for s in scenarios} - known)
        if unknown:
            raise ScenarioCatalogError(
                f"unknown variant_id(s) {unknown}; not in examples/ (init registry). "
                "Add the example tree or set require_known_variants=False for a "
                "downstream overlay that is not an init variant."
            )

    selected = enabled_ids if enabled_ids is not None else _ids_from_env()
    if selected is None:
        return scenarios
    by_id = {s.id: s for s in scenarios}
    missing = [sid for sid in selected if sid not in by_id]
    if missing:
        raise ScenarioCatalogError(f"unknown scenario id(s) {missing} in filter")
    return [by_id[sid] for sid in selected]


def _ids_from_env() -> list[str] | None:
    raw = os.environ.get(_ENV_FILTER, "").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _bundled_catalog_path() -> Path:
    catalog = _resources.files("agent_foundry.testing").joinpath(_DEFAULT_CATALOG)
    with _resources.as_file(catalog) as concrete:
        if not concrete.is_file():
            raise ScenarioCatalogError(f"bundled catalog missing at {concrete}")
        return Path(concrete)


def _read_catalog(path: Path) -> list[object]:
    try:
        with path.open("rb") as fh:
            parsed = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ScenarioCatalogError(f"failed to read {path}: {exc}") from exc
    rows = parsed.get("scenarios")
    if not isinstance(rows, list):
        raise ScenarioCatalogError(f"{path}: expected [[scenarios]] table array")
    return rows


__all__ = [
    "BLANK_VARIANT",
    "Capability",
    "Kind",
    "Lane",
    "Scenario",
    "ScenarioCatalogError",
    "load_scenarios",
]
