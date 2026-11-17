"""
Scorer Registry
===============
Name-to-scorer lookup for ``scorers.custom`` and
``production_monitoring.scorers`` in ``eval_config.yml``.

Merges the framework's code-based scorers with any ``Scorer`` objects
(e.g. ``@scorer`` functions) defined in an instance's
``evaluation/scorers/*.py`` modules.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from mlflow.genai.scorers import Scorer

from bricks.evaluation.scorers import CUSTOM_SCORERS

_INSTANCE_MODULE_PREFIX = "_af_instance_scorers_"


def _load_module(path: Path) -> ModuleType:
    """Import a single instance scorer file by path, without touching ``sys.path``."""
    module_name = f"{_INSTANCE_MODULE_PREFIX}{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load instance scorer module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise RuntimeError(f"Failed to import instance scorer module {path}: {exc}") from exc
    return module


def _module_scorers(module: ModuleType) -> list[Scorer]:
    return [obj for obj in vars(module).values() if isinstance(obj, Scorer)]


def discover_instance_scorers(instance_dir: Path | None) -> dict[str, Scorer]:
    """Collect ``Scorer`` objects from ``<instance_dir>/evaluation/scorers/*.py``.

    Files starting with ``_`` are skipped. Raises ``RuntimeError`` when a
    module fails to import and ``ValueError`` when two different scorers
    share a name.
    """
    if instance_dir is None:
        return {}
    scorers_dir = instance_dir / "evaluation" / "scorers"
    if not scorers_dir.is_dir():
        return {}

    found: dict[str, Scorer] = {}
    for path in sorted(scorers_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        for obj in _module_scorers(_load_module(path)):
            existing = found.get(obj.name)
            if existing is not None and existing is not obj:
                raise ValueError(f"Duplicate instance scorer name '{obj.name}' in {scorers_dir}")
            found[obj.name] = obj
    return found


def build_custom_registry(instance_dir: Path | None) -> dict[str, Scorer]:
    """Framework scorers merged with the instance's scorers, keyed by name.

    Raises ``ValueError`` when an instance scorer reuses a framework
    scorer's name. Framework scorers re-exported by an instance module
    (same object) are not treated as a clash.
    """
    registry: dict[str, Scorer] = {s.name: s for s in CUSTOM_SCORERS}
    for name, obj in discover_instance_scorers(instance_dir).items():
        framework = registry.get(name)
        if framework is obj:
            continue
        if framework is not None:
            raise ValueError(f"Instance scorer '{name}' shadows a framework scorer; rename it")
        registry[name] = obj
    return registry


__all__ = ["build_custom_registry", "discover_instance_scorers"]
