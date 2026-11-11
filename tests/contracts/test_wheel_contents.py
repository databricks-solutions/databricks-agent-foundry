"""Wheel-contents assertions (issue #112).

Pins what MUST end up inside the built wheel.  Catches MANIFEST.in /
``[tool.setuptools.package-data]`` drift loud and fast (no need to run
the full ``test_wheel_install.py`` integration test to spot a missing
override file).

The test builds a fresh wheel via :func:`agent_foundry.deploy.staging.build_deploy_staging`
into a temp dir, then walks the wheel's contents with :mod:`zipfile`.
This is the same code path used by ``agent-foundry deploy``, so a
green test means real deploys ship the same bits.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from agent_foundry.deploy.staging import build_deploy_staging

# Representative override files that exist today.  If you rename or
# remove these, update the list -- but think hard about whether the
# wheel-content contract is still being honoured for the rest of the
# tree.
_REQUIRED_OVERRIDE_FILES: tuple[str, ...] = (
    "payloads/agent/ui/overrides/MANIFEST.md",
    "payloads/agent/ui/overrides/client/package.json",
    "payloads/agent/ui/overrides/client/src/index.css",
    "payloads/agent/ui/overrides/client/src/components/elements/chart-spec.ts",
    "payloads/agent/ui/overrides/client/src/components/elements/interactive-chart.tsx",
    "payloads/agent/ui/overrides/server/src/routes/chat.ts",
)

# Brick package data shipped via MANIFEST.in -- the existing 68-A
# guarantees that brick scripts can load these via importlib.resources
# from a wheel install.  Pin a representative subset so a regression
# in the recursive-include glob is caught here.
_REQUIRED_BRICK_DATA_FILES: tuple[str, ...] = (
    "bricks/app/app.yaml",
    "bricks/app/_scaffold/config.yml",
    # Agent payload manifest scaffold -- payload-owned, not under the
    # generic ``app`` host brick (issue #338).
    "payloads/agent/_scaffold/payload.yml",
)

# Named end-to-end variants for ``agent-foundry init`` (#210).  A
# representative subset is pinned here so MANIFEST.in / package-data
# drift fails fast rather than at runtime when a user runs ``init``
# against a wheel install and gets a half-empty generated repo.
_REQUIRED_VARIANT_FILES: tuple[str, ...] = (
    "examples/portfolio_assistant/foundry.toml",
    "examples/portfolio_assistant/manifest.toml",
    "examples/portfolio_assistant/payloads/agent/payload.yml",
    "examples/portfolio_assistant/warehouse/config.yml",
    "examples/portfolio_assistant/payloads/agent/skills/catalog-explorer/SKILL.md",
    "examples/portfolio_assistant/evaluation/eval_config.yml",
    "examples/portfolio_assistant/tests/unit/conftest.py",
    "examples/portfolio_assistant/smoke.yml",
)

_REQUIRED_PLUGIN_FILES: tuple[str, ...] = (
    "agent_foundry/pytest_plugin.py",
    "agent_foundry/testing/scenarios.toml",
    "agent_foundry/testing/harness.py",
)

_REQUIRED_CONSTRAINT_FILES: tuple[str, ...] = (
    "agent_foundry/constraints/runtime.txt",
    "agent_foundry/constraints/extraction.txt",
    "agent_foundry/constraints/rest_mcp.txt",
)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a wheel once for the whole module.

    Reuses :func:`build_deploy_staging` so we exercise the same build
    invocation that the deploy code path uses -- if the deploy build
    drops a file, this test does too (and vice versa).  Using a
    synthetic instance dir keeps the helper happy without depending on
    the framework repo having an ``instances/`` tree.
    """
    inst = tmp_path_factory.mktemp("wheel_inst") / "synthetic"
    inst.mkdir()
    (inst / "foundry.toml").write_text('name = "synthetic"\nschema_prefix = "synthetic"\n', encoding="utf-8")

    handle = build_deploy_staging(inst)
    # Copy the wheel out before cleanup so the fixture survives.
    staging = Path(handle.name)
    wheel_src = next(staging.glob("*.whl"))
    wheel_copy = tmp_path_factory.mktemp("wheel_out") / wheel_src.name
    wheel_copy.write_bytes(wheel_src.read_bytes())
    handle.cleanup()
    return wheel_copy


def _wheel_member_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


@pytest.mark.parametrize("expected", _REQUIRED_OVERRIDE_FILES)
def test_chat_ui_overrides_ship_in_wheel(expected: str, built_wheel: Path):
    """Issue #112 belt-and-braces: the explicit
    ``[tool.setuptools.package-data]`` block in ``pyproject.toml`` MUST
    keep shipping the chat-UI override tree even when MANIFEST.in
    shifts.  An earlier dist wheel contained only ``MANIFEST.md`` --
    that silent regression would strip the entire customised UI from
    wheel-installed App deploys."""
    members = _wheel_member_names(built_wheel)
    assert expected in members, (
        f"override file {expected!r} missing from wheel; package-data "
        f"globbing regressed.  Wheel contents (first 40):\n" + "\n".join(sorted(members)[:40])
    )


@pytest.mark.parametrize("expected", _REQUIRED_BRICK_DATA_FILES)
def test_brick_package_data_ships_in_wheel(expected: str, built_wheel: Path):
    """68-A pinned that brick YAML / JSON templates ship as package
    data so brick scripts can load them via ``importlib.resources``.
    A representative subset is asserted here so a regression in
    MANIFEST.in's ``recursive-include`` glob fails fast."""
    members = _wheel_member_names(built_wheel)
    assert expected in members, (
        f"brick data file {expected!r} missing from wheel; MANIFEST.in recursive-include glob regressed."
    )


@pytest.mark.parametrize("expected", _REQUIRED_VARIANT_FILES)
def test_init_variant_ships_in_wheel(expected: str, built_wheel: Path):
    """The init wizard copies a variant tree from
    ``examples/<variant>/`` into the generated repo.  If the variant
    is missing from the wheel, the user gets a half-empty generated
    tree and a confusing error from ``importlib.resources.as_file``.
    Pin a representative subset of files (across multiple bricks,
    including the ``tests/`` subdir that needed an explicit ``*.py``
    add to MANIFEST.in) so drift fails the build, not the runtime."""
    members = _wheel_member_names(built_wheel)
    assert expected in members, (
        f"variant file {expected!r} missing from wheel; "
        "MANIFEST.in / [tool.setuptools.package-data] glob regressed "
        "for examples/ (#210)."
    )


def test_pytest_plugin_ships_in_wheel(built_wheel: Path):
    """Issue #434: init consumers get the skip gate from the wheel's
    pytest11 plugin, not a scaffolded conftest.py."""
    members = _wheel_member_names(built_wheel)
    for expected in _REQUIRED_PLUGIN_FILES:
        assert expected in members, (
            f"pytest plugin {expected!r} missing from wheel; consumers "
            "would collect integration tests on a unit pytest -q run."
        )


@pytest.mark.parametrize("expected", _REQUIRED_CONSTRAINT_FILES)
def test_app_constraints_ship_in_wheel(expected: str, built_wheel: Path):
    """Lock-derived App pins must be inside the wheel so child-repo
    deploys (no framework uv.lock) still stage ``-c`` files."""
    members = _wheel_member_names(built_wheel)
    assert expected in members, (
        f"constraint file {expected!r} missing from wheel; "
        "[tool.setuptools.package-data] glob for agent_foundry.constraints "
        "regressed."
    )


def test_wheel_does_not_ship_python_test_modules(built_wheel: Path):
    """``[tool.setuptools.packages.find].exclude`` keeps Python test
    subpackages (``bricks/<x>/tests/*.py``) out of the wheel -- they
    are dev-only and bloat the install otherwise.

    Note: JS / TS test fixtures under ``payloads/agent/ui/overrides/
    client/tests/`` ARE shipped, intentionally -- they are part of the
    chat-UI override payload, not Python test modules.  Same logic
    applies to the variant trees under ``examples/<variant>/tests/``
    -- those are downstream user tests that ``agent-foundry init``
    copies into the generated repo, not framework tests (#210).
    This assertion is therefore Python-specific AND restricted to
    framework-internal locations.
    """
    members = _wheel_member_names(built_wheel)
    py_test_members = [m for m in members if "/tests/" in m and m.endswith(".py") and not m.startswith("examples/")]
    assert not py_test_members, (
        "wheel ships Python test modules (should be excluded by "
        f"[tool.setuptools.packages.find].exclude): {py_test_members[:10]}"
    )
