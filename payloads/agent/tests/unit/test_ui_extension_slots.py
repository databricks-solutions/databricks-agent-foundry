"""UI extension slots are read at render time, never at module load.

An overlay registry may import core components (e.g. a route rendering
``Chat``), so a module-level read of ``chatUiExtensions`` can run while the
registry is still uninitialized and crash the app with a blank page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAYLOADS = Path(__file__).resolve().parents[3]
UI_SOURCES = sorted(
    path
    for path in PAYLOADS.glob("*/ui/overrides/client/src/**/*.ts*")
    if path.name != "registry.tsx" and "node_modules" not in path.parts
)
MODULE_LEVEL_READ = re.compile(r"^(?!import\b)\S.*\bchatUiExtensions\b")


def test_scans_the_core_slot_consumers() -> None:
    names = {path.name for path in UI_SOURCES}
    assert {"chat.tsx", "app-sidebar.tsx", "suggested-actions.tsx"} <= names


@pytest.mark.parametrize("path", UI_SOURCES, ids=lambda path: str(path.relative_to(PAYLOADS)))
def test_no_module_level_registry_read(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [f"{number}: {line}" for number, line in enumerate(lines, 1) if MODULE_LEVEL_READ.match(line)]
    assert not offenders, "read chatUiExtensions inside the component instead:\n" + "\n".join(offenders)
