"""End-to-end CLI tests for ``agent-foundry init`` (issue #210).

Drives the actual Typer-wired :func:`init_cmd` against ``tmp_path``
via :class:`typer.testing.CliRunner` with stdin-scripted prompt
answers.  Complements the unit tests in :mod:`tests.init.test_wizard`
(scripted IO adapter, no CLI involvement) and
:mod:`tests.init.test_renderer` (hand-built InitContext, no prompts).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_foundry._meta import DiscoveryResult
from agent_foundry.deploy.cli import app as cli_app
from agent_foundry.pytest_plugin import strip_ansi


def _record_path(bucket: list[Path], target: Path) -> bool:
    bucket.append(target)
    return True


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def stub_uv_lock_and_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not run real ``uv lock`` / ``uv sync`` during CLI tests.

    Those need network and a resolvable framework pin. Tests that
    care about install sequencing override these stubs.
    """
    monkeypatch.setattr("agent_foundry.deploy.cli.init._run_uv_lock", lambda _target: True)
    monkeypatch.setattr("agent_foundry.deploy.cli.init._run_uv_sync", lambda _target: True)


@pytest.fixture
def patched_discovery(monkeypatch: pytest.MonkeyPatch):
    """Helper to mock SHA discovery + install-url lookup.

    The CLI verb calls both :func:`discover_framework_sha` and the
    private ``_derive_install_url`` helper.  Tests use this fixture
    to stamp deterministic values into the generated pyproject.toml.
    """

    def _patch(*, sha: str | None, url: str) -> None:
        monkeypatch.setattr(
            "agent_foundry._meta.discover_framework_sha",
            lambda: DiscoveryResult(sha=sha, requested_ref="main", reason=""),
        )
        monkeypatch.setattr(
            "agent_foundry.deploy.cli.init._derive_install_url",
            lambda _ref: url,
        )

    return _patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_HOST = "https://wks.cloud.databricks.com"


def _stdin(*lines: str) -> str:
    """Join scripted prompt answers into a single stdin payload.

    Empty strings are kept literal so ``""`` exercises the
    "press enter to accept default" branch.
    """
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_init_creates_blank_github_repo(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    stdin = _stdin(
        "my-agent",  # project name
        _DEFAULT_HOST,
        "dev_cat",  # dev target
        "",  # CLI profile (optional)
        "n",  # add staging? no
        "n",  # add prod? no
        "git",  # ci provider
        "blank",  # starter
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=stdin)

    assert result.exit_code == 0, result.output
    generated = tmp_path / "my-agent"
    assert (generated / "pyproject.toml").is_file()
    assert (generated / "instances/my-agent/foundry.toml").is_file()
    assert (generated / ".github/workflows/pr-to-main.yml").is_file()
    assert not (generated / ".gitlab-ci.yml").exists()
    assert "Done. Your repo is at" in result.output
    assert "agent-foundry deploy --target dev" in result.output


def test_init_creates_full_sample_gitlab_repo(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="deadbeef" + "00" * 6, url="git+ssh://git@github.com/example/agent-foundry.git")

    stdin = _stdin(
        "portfolio",
        _DEFAULT_HOST,
        "portfolio_dev",
        "",
        "y",  # add staging
        "",
        "portfolio_staging",  # reuse dev host, distinct catalog
        "",
        "y",  # add prod
        "",
        "portfolio_prod",
        "",
        "gitlab",
        "portfolio_assistant",
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=stdin)

    assert result.exit_code == 0, result.output
    generated = tmp_path / "portfolio"
    assert (generated / ".gitlab-ci.yml").is_file()
    assert (generated / "instances/portfolio/payloads/agent/payload.yml").is_file()
    assert (generated / "instances/portfolio/warehouse/config.yml").is_file()

    pyproject = (generated / "pyproject.toml").read_text()
    assert "@deadbeef000000000000" in pyproject


# ---------------------------------------------------------------------------
# SHA-discovery fallback
# ---------------------------------------------------------------------------


def test_init_falls_back_to_main_when_sha_unknown(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha=None, url="git+ssh://git@github.com/example/agent-foundry.git")

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=stdin)

    assert result.exit_code == 0, result.output
    assert "could not discover" in result.output, "missing SHA must emit a warning"

    pyproject = (tmp_path / "my-agent/pyproject.toml").read_text()
    assert "@main" in pyproject


# ---------------------------------------------------------------------------
# --name override + refusal semantics
# ---------------------------------------------------------------------------


def test_init_name_flag_overrides_wizard_answer(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    stdin = _stdin(
        "wizard-said",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(
        cli_app,
        ["init", "--name", "cli-said", "--outdir", str(tmp_path)],
        input=stdin,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "cli-said").is_dir()
    assert not (tmp_path / "wizard-said").exists()
    assert "overrides the wizard's collected" in result.output


def test_init_refuses_non_empty_outdir(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    existing = tmp_path / "my-agent"
    existing.mkdir()
    (existing / "stale.txt").write_text("stale")

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=stdin)

    assert result.exit_code == 2
    assert "not empty" in result.output


def test_init_force_overwrites_non_empty_outdir(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    existing = tmp_path / "my-agent"
    existing.mkdir()
    (existing / "stale.txt").write_text("stale")

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--force"],
        input=stdin,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "my-agent/pyproject.toml").is_file()


# ---------------------------------------------------------------------------
# --help surfaces init
# ---------------------------------------------------------------------------


def test_init_appears_in_help(runner: CliRunner) -> None:
    """The top-level ``agent-foundry --help`` must list ``init`` so
    users discover it without reading the README.
    """
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_init_help_describes_flags(runner: CliRunner) -> None:
    result = runner.invoke(cli_app, ["init", "--help"])
    assert result.exit_code == 0
    output = strip_ansi(result.output)
    for token in ("--name", "--outdir", "--force", "--framework-ref", "--no-lock", "--no-sync"):
        assert token in output


# ---------------------------------------------------------------------------
# --framework-ref override (#211)
# ---------------------------------------------------------------------------


def test_init_framework_ref_overrides_discovery_when_sha_unknown(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    """An editable / local-path framework install yields
    ``discovery.sha=None``; ``--framework-ref`` must let the user pin
    a real commit anyway, suppressing the ``main`` fallback warning.
    """
    patched_discovery(sha=None, url="git+ssh://git@github.com/example/agent-foundry.git")
    pinned = "f" * 40

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--framework-ref", pinned],
        input=stdin,
    )

    assert result.exit_code == 0, result.output
    assert "could not discover" not in result.output, "--framework-ref must suppress the discovery-failure warning"
    pyproject = (tmp_path / "my-agent/pyproject.toml").read_text()
    assert f"@{pinned}" in pyproject
    assert "@main" not in pyproject


def test_init_framework_ref_warns_when_value_is_not_sha_shaped(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    """Catch the moving-target mistake -- ``--framework-ref main``
    succeeds (forks may rebrand default branches, so we don't reject)
    but emits a stderr warning so the user notices.
    """
    patched_discovery(sha=None, url="git+ssh://git@github.com/example/agent-foundry.git")

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--framework-ref", "main"],
        input=stdin,
    )

    assert result.exit_code == 0, result.output
    assert "does not look like a git SHA" in result.output


def test_init_framework_ref_overrides_discovered_sha(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    """Even when discovery succeeds, an explicit ``--framework-ref``
    wins.  Mirrors the existing ``--name`` override behavior so the
    user can pin a *different* SHA than the one they happen to be
    running.
    """
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")
    override = "b" * 40

    stdin = _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        "",
        "n",
        "n",
        "git",
        "blank",
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--framework-ref", override],
        input=stdin,
    )

    assert result.exit_code == 0, result.output
    assert "overrides the discovered SHA" in result.output
    pyproject = (tmp_path / "my-agent/pyproject.toml").read_text()
    assert f"@{override}" in pyproject
    assert "@abc1234" not in pyproject


def test_init_framework_ref_rejects_empty_string(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--framework-ref", "   "],
    )

    assert result.exit_code == 2
    assert "must not be empty" in result.output


# ---------------------------------------------------------------------------
# uv lock / uv sync during init
# ---------------------------------------------------------------------------


def _init_answers(*, profile: str = "") -> str:
    return _stdin(
        "my-agent",
        _DEFAULT_HOST,
        "dev_cat",
        profile,
        "n",
        "n",
        "git",
        "blank",
    )


def test_init_default_installs_and_omits_uv_sync_from_next_steps(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")
    lock_targets: list[Path] = []
    sync_targets: list[Path] = []
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_lock",
        lambda target: _record_path(lock_targets, target),
    )
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_sync",
        lambda target: _record_path(sync_targets, target),
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=_init_answers())

    assert result.exit_code == 0, result.output
    generated = tmp_path / "my-agent"
    assert lock_targets == [generated]
    assert sync_targets == [generated]
    next_steps = result.output.split("Next steps:", 1)[1]
    assert "uv sync" not in next_steps
    assert "uv lock" not in next_steps
    assert "databricks auth login" in next_steps
    assert "agent-foundry deploy --target dev" in next_steps


def test_init_profile_is_written_and_used_for_login(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path)],
        input=_init_answers(profile="DEFAULT"),
    )

    assert result.exit_code == 0, result.output
    foundry_toml = (tmp_path / "my-agent/foundry.toml").read_text()
    assert 'profile = "DEFAULT"' in foundry_toml
    assert f"databricks auth login --host {_DEFAULT_HOST} --profile DEFAULT" in result.output


def test_init_no_sync_tells_user_to_install(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")
    sync_calls: list[Path] = []
    monkeypatch.setattr("agent_foundry.deploy.cli.init._run_uv_lock", lambda _target: True)
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_sync",
        lambda target: _record_path(sync_calls, target),
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--no-sync"],
        input=_init_answers(),
    )

    assert result.exit_code == 0, result.output
    assert sync_calls == []
    next_steps = result.output.split("Next steps:", 1)[1]
    assert "uv sync" in next_steps
    assert "uv lock" not in next_steps


def test_init_skips_sync_when_lock_fails(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")
    sync_calls: list[Path] = []
    monkeypatch.setattr("agent_foundry.deploy.cli.init._run_uv_lock", lambda _target: False)
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_sync",
        lambda target: _record_path(sync_calls, target),
    )

    result = runner.invoke(cli_app, ["init", "--outdir", str(tmp_path)], input=_init_answers())

    assert result.exit_code == 0, result.output
    assert sync_calls == []
    next_steps = result.output.split("Next steps:", 1)[1]
    assert "uv lock" in next_steps
    assert "uv sync" in next_steps


def test_init_no_lock_skips_sync(
    runner: CliRunner,
    tmp_path: Path,
    patched_discovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched_discovery(sha="abc1234", url="git+ssh://git@github.com/example/agent-foundry.git")
    lock_calls: list[Path] = []
    sync_calls: list[Path] = []
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_lock",
        lambda target: _record_path(lock_calls, target),
    )
    monkeypatch.setattr(
        "agent_foundry.deploy.cli.init._run_uv_sync",
        lambda target: _record_path(sync_calls, target),
    )

    result = runner.invoke(
        cli_app,
        ["init", "--outdir", str(tmp_path), "--no-lock"],
        input=_init_answers(),
    )

    assert result.exit_code == 0, result.output
    assert lock_calls == []
    assert sync_calls == []
