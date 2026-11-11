"""Tests for :mod:`agent_foundry.init.renderer`.

Golden-file style: each test drives :func:`render` against a
``tmp_path`` and asserts on the resulting directory tree + the
content of the files that matter.  The (starter_template x
ci_provider) matrix gives four end-to-end cases; the remaining
tests cover refusal semantics, template substitution edge cases,
and the SHA-passthrough into ``pyproject.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry.init import (
    InitContext,
    InitRenderError,
    TargetSpec,
    render,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(
    *,
    project_name: str = "my-agent",
    starter: str = "blank",
    ci_provider: str = "git",
    with_staging: bool = False,
    with_prod: bool = False,
    dev_profile: str | None = None,
    framework_sha: str | None = "abc1234",
    framework_install_url: str = "git+ssh://git@github.com/example/agent-foundry.git",
) -> InitContext:
    """Build a representative :class:`InitContext` for renderer tests."""
    dev = TargetSpec(
        host="https://wks.cloud.databricks.com",
        catalog="dev_cat",
        profile=dev_profile,
    )
    staging = TargetSpec(host="https://wks.cloud.databricks.com", catalog="staging_cat") if with_staging else None
    prod = TargetSpec(host="https://wks.cloud.databricks.com", catalog="prod_cat") if with_prod else None
    return InitContext(
        project_name=project_name,
        dev=dev,
        staging=staging,
        prod=prod,
        ci_provider=ci_provider,  # type: ignore[arg-type]
        starter_template=starter,  # type: ignore[arg-type]
        framework_sha=framework_sha,
        framework_install_url=framework_install_url,
    )


# ---------------------------------------------------------------------------
# Directory tree -- (starter x ci_provider) matrix
# ---------------------------------------------------------------------------


def test_renders_blank_github_tree(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="blank", ci_provider="git", with_staging=True, with_prod=True), outdir)

    expected = {
        outdir / "pyproject.toml",
        outdir / "foundry.toml",
        outdir / "foundry.toml.example",
        outdir / ".gitignore",
        outdir / "README.md",
        outdir / "instances/my-agent/foundry.toml",
        outdir / ".github/workflows/pr-to-main.yml",
        outdir / ".github/workflows/pr-to-release.yml",
        outdir / ".github/workflows/release-deploy-prod.yml",
    }
    for path in expected:
        assert path.is_file(), f"missing generated file: {path}"
    assert not (outdir / ".gitlab-ci.yml").exists(), "GitLab CI must not be written when ci_provider=git"


def test_renders_blank_gitlab_tree(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="blank", ci_provider="gitlab", with_staging=True, with_prod=True), outdir)

    assert (outdir / ".gitlab-ci.yml").is_file()
    assert not (outdir / ".github").exists(), "GitHub workflows must not be written when ci_provider=gitlab"


def test_renders_full_sample_github_tree(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant", ci_provider="git"), outdir)

    instance_root = outdir / "instances/my-agent"
    assert (instance_root / "foundry.toml").is_file()
    assert (instance_root / "payloads/agent/payload.yml").is_file()
    assert (instance_root / "warehouse/config.yml").is_file()
    assert (instance_root / "payloads/agent/skills").is_dir()
    assert (instance_root / "evaluation/eval_config.yml").is_file()
    assert not (instance_root / "__init__.py").exists(), "package marker __init__.py must not leak into the user repo"
    assert not (instance_root / "manifest.toml").exists(), "variant manifest is registry metadata, not instance content"
    assert not (instance_root / "payloads/agent/skills/_assembled").exists(), (
        "build artifact must not ship in the variant tree"
    )


# ---------------------------------------------------------------------------
# Full-sample identity rewrite
# ---------------------------------------------------------------------------


def test_full_sample_rewrites_instance_name_and_schema_prefix(tmp_path: Path) -> None:
    """The vendored portfolio-assistant snapshot's per-instance
    foundry.toml hardcodes ``name = "portfolio-assistant"`` and
    ``schema_prefix = "portfolio_assistant"``.  After init the file
    must advertise the user's project name, otherwise deploys use
    the wrong UC schema prefix and Databricks Apps lists every
    init-generated agent under the same display name.
    """
    outdir = tmp_path / "test-agent"
    render(_ctx(project_name="test-agent", starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/test-agent/foundry.toml").read_text()
    assert 'name        = "test-agent"' in content
    assert 'schema_prefix = "test_agent"' in content
    # No stale identity values: not as a literal assigned value
    # (``name = "portfolio-assistant"``) nor as a hint
    # (``"portfolio-assistant-lakebase"``).  Mention in the
    # description / generated-from-the-portfolio-assistant comment is
    # acceptable -- those provide useful provenance.
    assert '"portfolio-assistant"' not in content
    assert "portfolio-assistant-lakebase" not in content
    assert '"portfolio_assistant"' not in content


def test_full_sample_rewrites_description_generically(tmp_path: Path) -> None:
    """The sample's description (``Agent with Genie Space analytics
    and Vector Search document retrieval``) is portfolio-specific
    and would mis-describe the user's agent.  Rewrite generically.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/my-agent/foundry.toml").read_text()
    assert "Agent with Genie Space" not in content
    assert "Portfolio Assistant" in content


def test_full_sample_preserves_warehouse_block(
    tmp_path: Path,
) -> None:
    """The identity-only rewrite must NOT touch the ``[warehouse]``
    block the sample carries as an opinionated infrastructure default.
    Regression guard for "the regex got too greedy".  chat_history is
    no longer a foundry.toml block (issue #338) -- see the payload
    capabilities test below.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/my-agent/foundry.toml").read_text()
    assert "[warehouse]" in content
    assert 'mode = "dedicated"' in content
    # Runtime toggles (incl. chat_history) moved to payload capabilities.
    assert "[chat_history]" not in content
    # vs_endpoint_name is shared infra, not identity -- preserve as-is.
    assert 'vs_endpoint_name = "vs_docs_demo"' in content


def test_full_sample_carries_chat_history_capability(tmp_path: Path) -> None:
    """chat_history is authored in the agent payload capabilities
    (single source, issue #338), not foundry.toml.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant"), outdir)

    payload = (outdir / "instances/my-agent/payloads/agent/payload.yml").read_text()
    assert "chat_history:" in payload
    assert "enabled: true" in payload


def test_full_sample_preserves_comments_for_first_time_user_guidance(
    tmp_path: Path,
) -> None:
    """The vendored foundry.toml uses inline comments
    (``SAFE TO CHANGE`` / ``CHANGE WITH CARE``) to guide first-time
    users.  A naive ``tomllib``-based round-trip would lose them; the
    identity rewrite stays regex-based to preserve them.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/my-agent/foundry.toml").read_text()
    assert "SAFE TO CHANGE" in content
    assert "CHANGE WITH CARE" in content


def test_full_sample_rewrites_commented_lakebase_project_name_hint(
    tmp_path: Path,
) -> None:
    """The sample's ``capabilities.chat_history`` block carries a
    commented-out ``# project_name: "portfolio-assistant-lakebase"`` hint
    to help users discover the override (issue #338).  Rewrite to use the
    user's project name so the hint stays useful instead of looking like
    a stale sample reference.
    """
    outdir = tmp_path / "test-agent"
    render(_ctx(project_name="test-agent", starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/test-agent/payloads/agent/payload.yml").read_text()
    assert '# project_name: "test-agent-lakebase"' in content
    assert "portfolio-assistant-lakebase" not in content


def test_full_sample_preserves_schema_header_comment(tmp_path: Path) -> None:
    """The vendored foundry.toml leads with a
    ``#:schema ../../.agent-foundry/schemas/foundry-instance.schema.json``
    pragma that JetBrains / Taplo use for editor validation.  Must
    survive the identity rewrite.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant"), outdir)

    content = (outdir / "instances/my-agent/foundry.toml").read_text()
    assert content.startswith("#:schema ../../.agent-foundry/schemas/")


def test_renders_full_sample_gitlab_tree(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="portfolio_assistant", ci_provider="gitlab"), outdir)

    assert (outdir / ".gitlab-ci.yml").is_file()
    assert (outdir / "instances/my-agent/payloads/agent/skills").is_dir()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_renders_schemas_directory(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(), outdir)

    schemas_dir = outdir / ".agent-foundry/schemas"
    assert schemas_dir.is_dir()
    schemas = list(schemas_dir.glob("*.schema.json"))
    assert schemas, "sync-schemas must populate at least one JSON schema file"


# ---------------------------------------------------------------------------
# pyproject.toml content
# ---------------------------------------------------------------------------


def test_pyproject_pins_resolved_sha(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(framework_sha="deadbeef" + "00" * 6), outdir)

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "@deadbeef000000000000" in pyproject, "framework pin must include the discovered SHA"
    assert "agent-foundry[runtime,eval,dev]" in pyproject


def test_pyproject_rest_mcp_proxy_uses_lean_extras(tmp_path: Path) -> None:
    """rest_mcp_proxy must not pull agent runtime / eval extras."""
    outdir = tmp_path / "my-proxy"
    render(_ctx(starter="rest_mcp_proxy", framework_sha="deadbeef" + "00" * 6), outdir)

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "agent-foundry[rest_mcp,call,dev]" in pyproject
    assert "[runtime,eval,dev]" not in pyproject


def test_renders_python_version_pin(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(), outdir)

    assert (outdir / ".python-version").read_text() == "3.11\n"


def test_generated_repo_does_not_ship_root_conftest(tmp_path: Path) -> None:
    """Unit CI is ``pytest -q``; live checks are ``pytest -q -m live --run-live``.

    Those flags used to live only in the framework root conftest, which
    init never copied (#434). The pytest11 plugin on the pinned wheel
    is the gate; a generated root conftest.py would duplicate options.
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(), outdir)

    assert not (outdir / "conftest.py").exists()
    pyproject = (outdir / "pyproject.toml").read_text()
    assert "--run-live" in pyproject
    assert "pytest plugin" in pyproject


def test_pyproject_falls_back_to_main_when_sha_missing(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(framework_sha=None), outdir)

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "@main" in pyproject, "framework pin must fall back to ``main`` when discovery returned None"


def test_pyproject_falls_back_to_canonical_url_when_install_url_missing(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(framework_install_url=""), outdir)

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "databricks-solutions/databricks-agent-foundry" in pyproject


def test_pyproject_adds_git_prefix_to_bare_pep610_ssh_url(tmp_path: Path) -> None:
    """PEP 610 ``direct_url.json`` records VCS URLs without the
    ``git+`` prefix (the VCS type lives in ``vcs_info.vcs``).  pip /
    uv parsing the generated ``pyproject.toml`` require ``git+ssh://``
    -- without normalisation ``uv sync`` fails with ``relative path
    without a working directory``.  Regression guard for the actual
    runtime failure caught when running ``init`` from a real uvx
    install.
    """
    outdir = tmp_path / "my-agent"
    render(
        _ctx(
            framework_install_url="ssh://git@github.com/example/agent-foundry.git",
        ),
        outdir,
    )

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "git+ssh://git@github.com/example/agent-foundry.git" in pyproject
    # Negative assertion: the bare form must not appear as a
    # standalone token (it must always be wrapped in ``git+...``).
    assert " @ ssh://" not in pyproject


def test_pyproject_adds_git_prefix_to_bare_pep610_https_url(tmp_path: Path) -> None:
    """Same regression as the ssh case but for the
    ``git+https://`` install path some users will hit when
    GitHub-PAT auth is configured instead of SSH.
    """
    outdir = tmp_path / "my-agent"
    render(
        _ctx(
            framework_install_url="https://github.com/example/agent-foundry.git",
        ),
        outdir,
    )

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "git+https://github.com/example/agent-foundry.git" in pyproject


def test_pyproject_does_not_double_prefix_existing_git_url(tmp_path: Path) -> None:
    """When the caller already produces a well-formed ``git+ssh://``
    URL (the happy-path test fixtures, future PEP 610 changes that
    keep the prefix, ...) we must not stack ``git+git+`` on top.
    """
    outdir = tmp_path / "my-agent"
    render(
        _ctx(
            framework_install_url="git+ssh://git@github.com/example/agent-foundry.git",
        ),
        outdir,
    )

    pyproject = (outdir / "pyproject.toml").read_text()
    assert "git+ssh://git@github.com/example/agent-foundry.git" in pyproject
    assert "git+git+" not in pyproject


# ---------------------------------------------------------------------------
# foundry.toml content
# ---------------------------------------------------------------------------


def test_foundry_toml_only_emits_configured_targets(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=False, with_prod=False), outdir)

    foundry_toml = (outdir / "foundry.toml").read_text()
    assert "[targets.dev]" in foundry_toml
    assert "[targets.staging]" not in foundry_toml
    assert "[targets.prod]" not in foundry_toml
    assert "[[instances]]" in foundry_toml
    assert 'path = "instances/my-agent"' in foundry_toml


def test_foundry_toml_emits_all_three_targets_in_order(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=True, with_prod=True), outdir)

    foundry_toml = (outdir / "foundry.toml").read_text()
    dev_idx = foundry_toml.index("[targets.dev]")
    staging_idx = foundry_toml.index("[targets.staging]")
    prod_idx = foundry_toml.index("[targets.prod]")
    assert dev_idx < staging_idx < prod_idx, "target blocks must be emitted in dev->staging->prod order"


def test_foundry_toml_example_matches_foundry_toml(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=True), outdir)

    foundry_toml = (outdir / "foundry.toml").read_text()
    example = (outdir / "foundry.toml.example").read_text()
    assert foundry_toml == example, "foundry.toml.example is the canonical bootstrap copy of foundry.toml"


def test_foundry_toml_emits_optional_profile(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(dev_profile="dev-profile"), outdir)

    foundry_toml = (outdir / "foundry.toml").read_text()
    assert 'profile = "dev-profile"' in foundry_toml


# ---------------------------------------------------------------------------
# Per-instance content (blank scope)
# ---------------------------------------------------------------------------


def test_blank_instance_has_identity_foundry_toml_and_test_homes(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(starter="blank"), outdir)

    instance_root = outdir / "instances/my-agent"
    files = sorted(p.relative_to(instance_root) for p in instance_root.rglob("*") if p.is_file())
    assert files == [
        Path("foundry.toml"),
        Path("tests/live/.gitkeep"),
        Path("tests/unit/.gitkeep"),
    ], "blank starter must produce only foundry.toml and the empty tests/unit + tests/live homes"

    content = (instance_root / "foundry.toml").read_text()
    assert 'name          = "my-agent"' in content
    assert 'schema_prefix = "my_agent"' in content, (
        "schema_prefix must derive from project_name with hyphens->underscores"
    )


# ---------------------------------------------------------------------------
# CI file content
# ---------------------------------------------------------------------------


def test_github_workflows_substitute_project_name(tmp_path: Path) -> None:
    outdir = tmp_path / "portfolio"
    render(_ctx(project_name="portfolio", ci_provider="git"), outdir)

    pr_to_release = (outdir / ".github/workflows/pr-to-release.yml").read_text()
    release_prod = (outdir / ".github/workflows/release-deploy-prod.yml").read_text()

    assert "--instance portfolio" in pr_to_release
    assert "--instance portfolio" in release_prod
    assert "__PROJECT_NAME__" not in pr_to_release, "substitution sentinel must be replaced"
    assert "__PROJECT_NAME__" not in release_prod


def test_github_workflows_preserve_github_actions_expressions(tmp_path: Path) -> None:
    """GitHub Actions ``${{ secrets.* }}`` syntax must survive the
    substitution intact (the bug fixed by switching from ``.format``
    to ``.replace``).
    """
    outdir = tmp_path / "my-agent"
    render(_ctx(ci_provider="git"), outdir)

    pr_to_release = (outdir / ".github/workflows/pr-to-release.yml").read_text()
    assert "${{ secrets.DATABRICKS_HOST_STAGING }}" in pr_to_release
    assert "${{ secrets.DATABRICKS_CLIENT_ID_STAGING }}" in pr_to_release
    assert "${{ secrets.DATABRICKS_CLIENT_SECRET_STAGING }}" in pr_to_release


def test_gitlab_ci_substitutes_project_name(tmp_path: Path) -> None:
    outdir = tmp_path / "portfolio"
    render(_ctx(project_name="portfolio", ci_provider="gitlab"), outdir)

    gitlab_ci = (outdir / ".gitlab-ci.yml").read_text()
    assert "--instance portfolio --target staging" in gitlab_ci
    assert "--instance portfolio --target prod" in gitlab_ci
    assert "__PROJECT_NAME__" not in gitlab_ci


def test_gitlab_ci_preserves_dollar_variable_syntax(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(ci_provider="gitlab"), outdir)

    gitlab_ci = (outdir / ".gitlab-ci.yml").read_text()
    assert "${FRAMEWORK_REPO_TOKEN}" in gitlab_ci
    assert "$DATABRICKS_HOST_STAGING" in gitlab_ci


def test_github_staging_deploys_before_acceptance_and_does_not_destroy(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(ci_provider="git"), outdir)
    pr_to_release = (outdir / ".github/workflows/pr-to-release.yml").read_text()
    pr_to_main = (outdir / ".github/workflows/pr-to-main.yml").read_text()
    prod = (outdir / ".github/workflows/release-deploy-prod.yml").read_text()

    assert "databricks/setup-cli@" in pr_to_release
    assert "databricks/setup-cli@" in prod
    assert "environment:\n      name: staging" in pr_to_release
    deploy_at = pr_to_release.find("agent-foundry deploy --instance")
    accept_at = pr_to_release.find("-m live --run-live")
    assert 0 <= deploy_at < accept_at
    assert "destroy" not in pr_to_release
    assert "DATABRICKS_HOST" not in pr_to_main
    assert "environment:\n      name: prod" in prod
    assert "destroy" not in prod
    assert "--run-live" not in prod


def test_gitlab_staging_deploys_before_acceptance_and_does_not_destroy(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(ci_provider="gitlab"), outdir)
    gitlab_ci = (outdir / ".gitlab-ci.yml").read_text()
    deploy_at = gitlab_ci.find("deploy-staging:")
    accept_at = gitlab_ci.find("acceptance-staging:")
    assert 0 <= deploy_at < accept_at
    assert "-m live --run-live" in gitlab_ci[accept_at:]
    assert "when: manual" in gitlab_ci[deploy_at:accept_at]
    assert "agent-foundry destroy" not in gitlab_ci


# ---------------------------------------------------------------------------
# README content
# ---------------------------------------------------------------------------


def test_readme_lists_only_configured_targets(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=True, with_prod=False), outdir)

    readme = (outdir / "README.md").read_text()
    assert "| `dev` |" in readme
    assert "| `staging` |" in readme
    assert "| `prod` |" not in readme


def test_readme_emits_ci_secrets_for_configured_targets(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=True, with_prod=True), outdir)

    readme = (outdir / "README.md").read_text()
    assert "DATABRICKS_HOST_STAGING" in readme
    assert "DATABRICKS_CLIENT_ID_STAGING" in readme
    assert "DATABRICKS_CLIENT_SECRET_STAGING" in readme
    assert "DATABRICKS_HOST_PROD" in readme


def test_readme_handles_dev_only_repo_gracefully(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(with_staging=False, with_prod=False), outdir)

    readme = (outdir / "README.md").read_text()
    assert "DATABRICKS_HOST_STAGING" not in readme
    assert "no CI deploy targets configured" in readme


def test_readme_login_uses_configured_dev_profile(tmp_path: Path) -> None:
    outdir = tmp_path / "my-agent"
    render(_ctx(dev_profile="dev-profile"), outdir)

    readme = (outdir / "README.md").read_text()
    assert "databricks auth login --host https://wks.cloud.databricks.com --profile dev-profile" in readme


# ---------------------------------------------------------------------------
# Refusal semantics
# ---------------------------------------------------------------------------


def test_refuses_to_write_into_non_empty_dir(tmp_path: Path) -> None:
    outdir = tmp_path / "existing"
    outdir.mkdir()
    (outdir / "something.txt").write_text("not yours")

    with pytest.raises(InitRenderError, match="not empty"):
        render(_ctx(), outdir)


def test_writes_into_existing_empty_dir(tmp_path: Path) -> None:
    outdir = tmp_path / "existing"
    outdir.mkdir()

    render(_ctx(), outdir)

    assert (outdir / "pyproject.toml").is_file()


def test_force_overwrites_non_empty_dir(tmp_path: Path) -> None:
    outdir = tmp_path / "existing"
    outdir.mkdir()
    (outdir / "stale.txt").write_text("stale")

    render(_ctx(), outdir, force=True)

    assert (outdir / "pyproject.toml").is_file()
    # Pre-existing unrelated files survive force; only files we
    # actively wrote are touched.
    assert (outdir / "stale.txt").is_file()


def test_refuses_when_outdir_is_a_file(tmp_path: Path) -> None:
    outdir = tmp_path / "file-not-dir"
    outdir.write_text("oops")

    with pytest.raises(InitRenderError, match="not a directory"):
        render(_ctx(), outdir)
