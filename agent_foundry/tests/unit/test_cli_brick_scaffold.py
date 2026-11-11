"""Tests for ``agent-foundry scaffold <name>`` (PR 3 of typed-config).

Two test files cover the verb:

* This file: CLI-level dispatch, argument parsing, exit codes, file
  creation / skip semantics, and the redirect-only scaffolds.
* ``test_brick_scaffold_content.py``: validates that the
  Pydantic-backed scaffolds round-trip through their own model
  validators (i.e. the scaffolded file is itself valid against the
  framework's typed contract).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_foundry.deploy.cli import (
    app as cli_app,
)
from agent_foundry.deploy.cli.scaffold import _run_brick_scaffold
from agent_foundry.pytest_plugin import strip_ansi
from agent_foundry.scaffold import (
    available_scaffold_names,
    build_scaffold_registry,
    run_scaffold,
    scaffold_entries,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    """A minimal repo with a single empty instance directory.

    Real repos have a per-instance ``foundry.toml`` here; the
    scaffold verb does not require one because it does not need to
    resolve config (just the directory path).
    """
    inst = tmp_path / "instances" / "demo"
    inst.mkdir(parents=True)
    return inst


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


# Bricks that are intentionally NOT scaffoldable -- their config
# either lives elsewhere (foundry.toml) or has no user-authored
# surface at all.  Listing them is the source of truth for the
# coverage test below; any brick added here documents *why* it
# doesn't need a scaffold.
_NOT_SCAFFOLDABLE: dict[str, str] = {
    "observability": "no user-authored config (dashboard is generated)",
    "tracing": "no user-authored config (experiment is computed from foundry.toml)",
}


def test_every_brick_either_scaffolds_or_is_explicitly_excluded() -> None:
    """Every built-in brick must either have a scaffold *or* be in
    ``_NOT_SCAFFOLDABLE``.  This catches the failure mode where
    someone adds a brick and forgets to decide whether it should be
    in ``scaffold --list``.
    """
    from agent_foundry.bricks import iter_bricks

    brick_names = {b.name for b in iter_bricks()}
    scaffold_names = set(available_scaffold_names())
    excluded = set(_NOT_SCAFFOLDABLE)

    missing = brick_names - scaffold_names - excluded
    assert not missing, (
        f"Bricks unaccounted for: add a ScaffoldDecl in "
        f"agent_foundry/scaffold.py *or* to _NOT_SCAFFOLDABLE in "
        f"this test file with a one-line reason: {sorted(missing)}"
    )

    # Don't allow a brick to be in both -- that's contradictory.
    contradictory = scaffold_names & excluded
    assert not contradictory, f"Bricks both scaffolded and marked not-scaffoldable: {sorted(contradictory)}"


def test_available_scaffold_names_is_sorted() -> None:
    """Stable ordering keeps the CLI help string deterministic."""
    names = available_scaffold_names()
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# File scaffolds: write / skip / force semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brick_name,expected_relpath",
    [
        ("warehouse", "warehouse/config.yml"),
        ("evaluation", "evaluation/eval_config.yml"),
        ("evaluation", "evaluation/datasets/smoke.yml"),
        # ``app`` is the generic host: it scaffolds infra only (#338).
        ("app", "app/config.yml"),
        # The agent runtime manifest is a payload-owned scaffold verb
        # contributed by the agent adapter (#368), not part of ``app``.
        ("payload", "payloads/agent/payload.yml"),
    ],
)
def test_pydantic_scaffold_writes_expected_file(
    instance_dir: Path,
    brick_name: str,
    expected_relpath: str,
) -> None:
    result = run_scaffold(brick_name, instance_dir)
    assert (instance_dir / expected_relpath).exists()
    assert (instance_dir / expected_relpath) in result.written
    assert result.skipped == []


@pytest.mark.parametrize(
    "brick_name",
    ["warehouse", "app", "evaluation"],
)
def test_pydantic_scaffold_includes_schema_header(
    instance_dir: Path,
    brick_name: str,
) -> None:
    """Editors discover the JSON Schema via the
    ``# yaml-language-server`` directive. Without it, autocomplete and
    validation silently degrade.
    """
    run_scaffold(brick_name, instance_dir)
    body = next(instance_dir.rglob("*.yml")).read_text(encoding="utf-8")
    assert "yaml-language-server" in body
    assert ".agent-foundry/schemas/" in body


@pytest.mark.parametrize(
    "brick_name,expected_relpath",
    [
        ("mcp", "payloads/agent/mcp_servers/example_server/server.yml"),
        ("vector_indexes", "vector_indexes/example_index/index_config.yml"),
        ("jobs", "jobs/example_job/job.yml"),
    ],
)
def test_directory_scaffold_includes_schema_header(
    instance_dir: Path,
    brick_name: str,
    expected_relpath: str,
) -> None:
    """Directory-shaped scaffolds (mcp / skills) also carry the
    ``# yaml-language-server`` directive so their authoring YAMLs get
    IDE autocomplete + validation (#323). The single-file Pydantic
    bricks are covered separately above.
    """
    run_scaffold(brick_name, instance_dir)
    body = (instance_dir / expected_relpath).read_text(encoding="utf-8")
    assert "yaml-language-server" in body
    assert ".agent-foundry/schemas/" in body


def test_scaffold_skips_existing_file_without_force(instance_dir: Path) -> None:
    (instance_dir / "warehouse").mkdir()
    target = instance_dir / "warehouse" / "config.yml"
    target.write_text("# user-authored\nsize: Medium\n", encoding="utf-8")

    result = run_scaffold("warehouse", instance_dir)

    assert result.written == []
    assert target in result.skipped
    # File untouched -- user content preserved:
    assert "user-authored" in target.read_text(encoding="utf-8")


def test_scaffold_force_overwrites_existing_file(instance_dir: Path) -> None:
    (instance_dir / "warehouse").mkdir()
    target = instance_dir / "warehouse" / "config.yml"
    target.write_text("# user-authored\nsize: Medium\n", encoding="utf-8")

    result = run_scaffold("warehouse", instance_dir, force=True)

    assert target in result.written
    assert result.skipped == []
    # File replaced -- starter template now in place:
    assert "yaml-language-server" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Directory-shaped scaffolds (copy template tree)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brick_name,expected_subdir",
    [
        ("genie_spaces", "genie_spaces/example_space"),
        ("vector_indexes", "vector_indexes/example_index"),
        ("jobs", "jobs/example_job"),
        # ``mcp`` brick.name vs ``mcp_servers/`` watched dir (now payload-owned):
        ("mcp", "payloads/agent/mcp_servers/example_server"),
        ("data_domains", "data_domains/example_domain"),
        # ``sql_function`` brick.name vs ``sql_functions/`` watched dir:
        ("sql_function", "sql_functions/example_functions"),
    ],
)
def test_template_scaffold_creates_subdir_with_files(
    instance_dir: Path,
    brick_name: str,
    expected_subdir: str,
) -> None:
    result = run_scaffold(brick_name, instance_dir)
    subdir = instance_dir / expected_subdir
    assert subdir.is_dir(), f"expected {subdir} to exist"
    # Every written path must be inside the expected sub-instance dir.
    for written in result.written:
        assert subdir in written.parents or written.parent == subdir
    # At least one file actually got written.
    assert result.written


def test_skills_scaffold_creates_a_valid_skill(instance_dir: Path) -> None:
    """The skills scaffold writes one ``SKILL.md`` skill that passes validation."""
    from payloads.agent.deploy.skills.validate import validate_skill_dir

    result = run_scaffold("skills", instance_dir)
    skill_dir = instance_dir / "payloads" / "agent" / "skills" / "my-skill"

    assert (skill_dir / "SKILL.md").is_file()
    assert result.written
    assert [e for e in validate_skill_dir(skill_dir) if e.severity == "error"] == []


# ---------------------------------------------------------------------------
# Excluded bricks (no scaffold registered)
# ---------------------------------------------------------------------------
#
# ``observability`` is a deploy-time brick but has no user-authored
# config worth scaffolding (the dashboard is generated).  These
# tests guard against accidental re-registration.  ``lakebase`` was
# removed entirely in the PR that folded its functionality into the
# Agent App brick.


@pytest.mark.parametrize("brick_name", list(_NOT_SCAFFOLDABLE))
def test_excluded_brick_is_not_scaffoldable(brick_name: str) -> None:
    assert brick_name not in available_scaffold_names()


@pytest.mark.parametrize("brick_name", list(_NOT_SCAFFOLDABLE))
def test_run_scaffold_rejects_excluded_brick(
    instance_dir: Path,
    brick_name: str,
) -> None:
    """Asking for a scaffold on an excluded brick must surface a
    KeyError so the CLI can render a friendly "unknown brick" message
    rather than silently doing nothing.
    """
    with pytest.raises(KeyError):
        run_scaffold(brick_name, instance_dir)


def test_app_scaffold_writes_infra_config_only(
    instance_dir: Path,
) -> None:
    """``app`` is the generic host: its scaffold writes infra config
    only (compute / resources / scopes).  The agent runtime manifest is
    the payload's own scaffold verb (#338, #368), so ``app`` must NOT
    emit ``payload.yml``.
    """
    result = run_scaffold("app", instance_dir)
    assert (instance_dir / "app" / "config.yml").exists()
    assert not (instance_dir / "payloads" / "agent" / "payload.yml").exists()
    assert result.written


def test_payload_scaffold_writes_agent_manifest(
    instance_dir: Path,
) -> None:
    """The agent adapter contributes a ``payload`` scaffold verb (#368)
    that writes the payload manifest under ``payloads/agent/``."""
    result = run_scaffold("payload", instance_dir)
    assert (instance_dir / "payloads" / "agent" / "payload.yml").exists()
    assert result.written


def test_agent_app_scaffold_removed_in_2_0(
    instance_dir: Path,
) -> None:
    """The deprecated ``agent_app`` scaffold alias was removed in
    agent-foundry 2.0 (issue #338); only ``app`` remains."""
    assert "agent_app" not in build_scaffold_registry()
    with pytest.raises(KeyError):
        run_scaffold("agent_app", instance_dir)


# ---------------------------------------------------------------------------
# Core dispatch (_run_brick_scaffold, keyword-args API)
# ---------------------------------------------------------------------------
#
# This is the implementation behind the top-level ``scaffold`` verb.


def test_core_dispatch_writes_files(
    instance_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = instance_dir.parent.parent
    rc = _run_brick_scaffold(
        brick_name="warehouse",
        instance="demo",
        repo_root=repo_root,
        force=False,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "wrote" in captured.out
    assert (instance_dir / "warehouse" / "config.yml").exists()


def test_core_dispatch_unknown_brick(
    instance_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = instance_dir.parent.parent
    rc = _run_brick_scaffold(
        brick_name="ghost",
        instance="demo",
        repo_root=repo_root,
        force=False,
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "Unknown brick" in captured.err
    # Must list the available names so the user can self-correct.
    assert "warehouse" in captured.err


def test_core_dispatch_missing_instance_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _run_brick_scaffold(
        brick_name="warehouse",
        instance="ghost",
        repo_root=tmp_path,
        force=False,
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "does not exist" in captured.err


def test_core_dispatch_force_overwrites(
    instance_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = instance_dir.parent.parent
    (instance_dir / "warehouse").mkdir()
    target = instance_dir / "warehouse" / "config.yml"
    target.write_text("# existing\n", encoding="utf-8")

    rc = _run_brick_scaffold(
        brick_name="warehouse",
        instance="demo",
        repo_root=repo_root,
        force=False,
    )
    assert rc == 0
    assert capsys.readouterr().out.count("skipped") == 1
    assert target.read_text(encoding="utf-8") == "# existing\n"

    rc = _run_brick_scaffold(
        brick_name="warehouse",
        instance="demo",
        repo_root=repo_root,
        force=True,
    )
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert "yaml-language-server" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Top-level ``agent-foundry scaffold`` verb (primary surface)
# ---------------------------------------------------------------------------
#
# Exercised end-to-end through Typer's CliRunner so the test covers
# argument parsing, help string, and exit codes -- not just the
# underlying dispatch function.


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def test_top_level_scaffold_writes_files(
    instance_dir: Path,
    runner: CliRunner,
) -> None:
    repo_root = instance_dir.parent.parent
    result = runner.invoke(
        cli_app,
        [
            "scaffold",
            "warehouse",
            "--instance",
            "demo",
            "--repo-root",
            str(repo_root),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "wrote" in result.stdout
    assert (instance_dir / "warehouse" / "config.yml").exists()


def test_top_level_scaffold_unknown_brick(
    instance_dir: Path,
    runner: CliRunner,
) -> None:
    repo_root = instance_dir.parent.parent
    result = runner.invoke(
        cli_app,
        [
            "scaffold",
            "ghost",
            "--instance",
            "demo",
            "--repo-root",
            str(repo_root),
        ],
    )
    assert result.exit_code == 2
    assert "Unknown brick" in result.stderr
    # Must list valid names so the user can self-correct.
    assert "warehouse" in result.stderr


def test_top_level_scaffold_missing_instance_flag(
    instance_dir: Path,
    runner: CliRunner,
) -> None:
    """Omitting ``--instance`` must fail with a brick-specific example
    line, not Typer's generic "Missing option" error.
    """
    del instance_dir  # only here for the fixture sequencing
    result = runner.invoke(cli_app, ["scaffold", "warehouse"])
    assert result.exit_code == 2
    # Custom error path, not Typer's stock missing-option message.
    assert "Missing --instance" in result.stderr
    # Echoes the brick the user already provided in the suggested fix.
    assert "scaffold warehouse --instance" in result.stderr


def test_top_level_scaffold_missing_brick_lists_available(
    runner: CliRunner,
) -> None:
    """Running ``scaffold`` with no brick must list valid choices --
    not just print Typer's generic "Missing argument" error.
    """
    result = runner.invoke(cli_app, ["scaffold"])
    assert result.exit_code == 2
    assert "Missing BRICK" in result.stderr
    # Listing must include scaffoldable bricks (one of each shape) so
    # the table is visible.
    assert "warehouse" in result.stderr
    assert "genie_spaces" in result.stderr


def test_top_level_scaffold_list_flag_prints_table(
    runner: CliRunner,
) -> None:
    """``--list`` prints the user-facing table -- brick name, display
    label, and one-line summary -- so users can pick by purpose, not
    by guessing names.
    """
    result = runner.invoke(cli_app, ["scaffold", "--list"])
    assert result.exit_code == 0
    out = result.stdout
    # Brick names (column 1).
    assert "warehouse" in out
    assert "genie_spaces" in out
    # Display labels (column 2) -- recognisable product names.
    assert "SQL Warehouse" in out
    assert "Genie Spaces" in out
    # Implementation jargon must be GONE -- users don't care whether
    # a scaffold is pydantic-typed or template-shaped.  This guards
    # against the categorisation headings accidentally creeping back
    # into the user-facing surface.
    assert "Pydantic-typed" not in out
    assert "Directory-shaped" not in out


def test_top_level_scaffold_help_mentions_scaffold(
    runner: CliRunner,
) -> None:
    """``agent-foundry --help`` lists the new top-level verb so authors
    can discover it without reading the source.
    """
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "scaffold" in result.stdout


def test_top_level_scaffold_help_points_at_list(
    runner: CliRunner,
) -> None:
    """``scaffold --help`` cannot inline the table cleanly because
    Typer reflows pre-formatted text.  Instead it must point users
    at ``scaffold --list`` so they can see the readable form.
    """
    result = runner.invoke(cli_app, ["scaffold", "--help"])
    assert result.exit_code == 0
    assert "scaffold --list" in strip_ansi(result.stdout)


# ---------------------------------------------------------------------------
# Removed alias: ``brick <name> scaffold``
# ---------------------------------------------------------------------------


def test_brick_scaffold_alias_is_rejected(
    instance_dir: Path,
    runner: CliRunner,
) -> None:
    """``brick <name> scaffold`` is no longer an alias for the top-level verb."""
    repo_root = instance_dir.parent.parent
    result = runner.invoke(
        cli_app,
        ["brick", "warehouse", "scaffold", "--instance", "demo", "--repo-root", str(repo_root)],
    )
    assert result.exit_code == 2
    assert not (instance_dir / "warehouse" / "config.yml").exists()


# ---------------------------------------------------------------------------
# Internal-API guard
# ---------------------------------------------------------------------------


def test_scaffolds_registry_keys_match_run_scaffold_dispatch() -> None:
    """``available_scaffold_names`` and the composed registry must agree
    on the set of supported names -- they're the two halves of the same
    contract.
    """
    assert set(build_scaffold_registry()) == set(available_scaffold_names())


def test_unknown_brick_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        run_scaffold("ghost", Path("."), force=False)


# ---------------------------------------------------------------------------
# Drift guard: scaffold target dir vs brick applies_to dir
# ---------------------------------------------------------------------------
#
# Some bricks watch a directory whose name differs from the brick's
# ``.name`` attribute (e.g. brick.name="mcp" but watches "mcp_servers/").
# When the scaffold writes to brick.name/ instead of the watched dir,
# the next ``agent-foundry deploy`` silently skips the brick because
# its applies_to() returns False on the empty watched dir.
#
# This regression guard verifies every directory-shaped scaffold writes
# *inside* a directory the brick actually watches, by running the
# scaffold and then asking the brick whether the instance now applies.


# ``skills`` and ``mcp`` are intentionally absent: they are no longer
# deploy bricks (issue #338) -- skill assembly and MCP registration are
# capabilities of the agent payload's provision manifest -- so there is
# no ``applies_to`` to drift-check.  Their scaffolds are still covered by
# ``test_skills_scaffold_creates_a_valid_skill`` and the ``mcp`` row
# in ``test_template_scaffold_creates_subdir_with_files`` above.
@pytest.mark.parametrize(
    "brick_name",
    [
        "genie_spaces",
        "vector_indexes",
        "jobs",
        "data_domains",
        "sql_function",
    ],
)
def test_directory_scaffold_makes_brick_applies_to_return_true(
    instance_dir: Path,
    brick_name: str,
) -> None:
    """After scaffolding a directory-shaped brick into a fresh
    instance, that brick's ``applies_to`` must return True -- otherwise
    the brick won't run when the operator deploys, defeating the point
    of the scaffold.
    """
    from agent_foundry.bricks import DeployContext, iter_bricks

    run_scaffold(brick_name, instance_dir)

    brick = next((b for b in iter_bricks() if b.name == brick_name), None)
    assert brick is not None, f"brick {brick_name!r} missing from registry"

    ctx = DeployContext(
        instance_dir=instance_dir,
        target="dev",
        variables={},
        dry_run=True,
    )
    assert brick.applies_to(ctx), (
        f"Scaffold wrote files for brick {brick_name!r} but its "
        f"applies_to() returned False -- the scaffold target dir does "
        f"not match what the brick actually watches.  Check "
        f"the ScaffoldDecl sources in agent_foundry.scaffold / the payload adapter."
    )


# ---------------------------------------------------------------------------
# Drift guard: listing entries vs. composed registry
# ---------------------------------------------------------------------------


def test_scaffold_entries_match_registry() -> None:
    """:func:`scaffold_entries` (listing order) and the composed
    registry (dispatch) must cover exactly the same set of names.

    Without this guard, a scaffold could be dispatchable but invisible
    in ``--help`` / ``--list`` (or vice versa).
    """
    entry_names = [decl.name for decl in scaffold_entries()]
    registry_names = sorted(build_scaffold_registry())
    assert sorted(entry_names) == registry_names, (
        f"scaffold_entries drifted from the registry. Registry: {registry_names}. Entries: {sorted(entry_names)}."
    )
    # Duplicate names would render the scaffold twice in the table.
    assert len(entry_names) == len(set(entry_names)), "Duplicate scaffold name in scaffold_entries()."


def test_scaffold_entries_have_non_empty_display_and_summary() -> None:
    """Every entry must carry a non-empty display label and summary --
    blank cells in the user-facing table would defeat the whole
    purpose of this surface.
    """
    for decl in scaffold_entries():
        assert decl.display.strip(), f"{decl.name!r}: empty display"
        assert decl.summary.strip(), f"{decl.name!r}: empty summary"
        # Summaries should be a single line -- multi-line breaks the
        # 3-column table layout.
        assert "\n" not in decl.summary, f"{decl.name!r}: summary spans multiple lines"
