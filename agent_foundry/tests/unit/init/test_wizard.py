"""Tests for :mod:`agent_foundry.init.wizard`.

The wizard is pure orchestration over an injectable IO adapter, so
the tests run entirely in-process with no filesystem or stdin
involvement.  A scripted :class:`_ScriptedIO` adapter replays canned
prompt answers and records emitted output so assertions can target
both the validated :class:`InitContext` and the user-visible
re-prompt behavior.
"""

from __future__ import annotations

from collections import deque

import pytest
from pydantic import ValidationError

from agent_foundry.init import (
    CIProvider,
    InitContext,
    StarterTemplate,
    TargetSpec,
    collect,
)
from agent_foundry.init.examples_registry import allowed_starter_templates

# ---------------------------------------------------------------------------
# Scripted IO adapter
# ---------------------------------------------------------------------------


class _ScriptedIO:
    """Test double implementing the :class:`InitIO` protocol.

    Prompt answers are popped from ``responses`` in order.  Confirm
    answers come from ``confirms``.  Echo lines are captured in
    ``echoes`` for assertions about user-visible messages (e.g. the
    blank-scope scaffold hint).
    """

    def __init__(
        self,
        *,
        responses: list[str],
        confirms: list[bool],
    ) -> None:
        self._responses: deque[str] = deque(responses)
        self._confirms: deque[bool] = deque(confirms)
        self.echoes: list[str] = []
        self.prompts: list[tuple[str, str | None]] = []

    def prompt(self, message: str, default: str | None = None) -> str:
        self.prompts.append((message, default))
        if not self._responses:
            raise AssertionError(f"ran out of scripted prompt responses at prompt: {message!r}")
        answer = self._responses.popleft()
        if answer == "" and default is not None:
            return default
        return answer

    def confirm(self, message: str, default: bool = False) -> bool:
        if not self._confirms:
            raise AssertionError(f"ran out of scripted confirm responses at prompt: {message!r}")
        return self._confirms.popleft()

    def echo(self, message: str = "") -> None:
        self.echoes.append(message)


# ---------------------------------------------------------------------------
# TargetSpec
# ---------------------------------------------------------------------------


def test_target_spec_validates_host() -> None:
    with pytest.raises(ValidationError, match="host"):
        TargetSpec(host="not-a-url", catalog="ok_cat")


def test_target_spec_validates_catalog() -> None:
    with pytest.raises(ValidationError, match="catalog"):
        TargetSpec(host="https://wks.cloud.databricks.com", catalog="UPPER")


def test_target_spec_accepts_hyphenated_catalog() -> None:
    # Unity Catalog permits hyphens in catalog names (backtick-quoted in SQL);
    # only ``.``, space, ``/`` and control chars are forbidden.
    catalog = "acme_platform-eu-dev"
    spec = TargetSpec(host="https://wks.cloud.databricks.com", catalog=catalog)
    assert spec.catalog == catalog


def test_target_spec_strips_host_trailing_slash() -> None:
    spec = TargetSpec(host="https://wks.cloud.databricks.com/", catalog="dev_cat")
    assert spec.host == "https://wks.cloud.databricks.com"


def test_target_spec_accepts_azure_host() -> None:
    # Azure Databricks workspaces use ``*.azuredatabricks.net`` rather than
    # the ``*.databricks.com`` suffix used by AWS/GCP.
    spec = TargetSpec(host="https://adb-1234567890123456.7.azuredatabricks.net/", catalog="dev_cat")
    assert spec.host == "https://adb-1234567890123456.7.azuredatabricks.net"


def test_target_spec_accepts_optional_profile() -> None:
    spec = TargetSpec(
        host="https://wks.cloud.databricks.com",
        catalog="dev_cat",
        profile="my-profile",
    )
    assert spec.profile == "my-profile"


def test_target_spec_rejects_unsafe_profile() -> None:
    with pytest.raises(ValidationError, match="profile"):
        TargetSpec(
            host="https://wks.cloud.databricks.com",
            catalog="dev_cat",
            profile='bad"]',
        )


# ---------------------------------------------------------------------------
# InitContext catalog-uniqueness
# ---------------------------------------------------------------------------


def _spec(host: str, catalog: str) -> TargetSpec:
    return TargetSpec(host=host, catalog=catalog)


def test_init_context_accepts_unique_catalogs() -> None:
    InitContext(
        project_name="my-agent",
        dev=_spec("https://wks.cloud.databricks.com", "dev_cat"),
        staging=_spec("https://wks.cloud.databricks.com", "staging_cat"),
        prod=_spec("https://wks.cloud.databricks.com", "prod_cat"),
        ci_provider="git",
        starter_template="blank",
    )


def test_init_context_rejects_duplicate_catalogs_dev_staging() -> None:
    with pytest.raises(ValidationError, match="unique"):
        InitContext(
            project_name="my-agent",
            dev=_spec("https://wks.cloud.databricks.com", "shared"),
            staging=_spec("https://wks.cloud.databricks.com", "shared"),
            ci_provider="git",
            starter_template="blank",
        )


def test_init_context_rejects_duplicate_catalogs_dev_prod() -> None:
    with pytest.raises(ValidationError, match="unique"):
        InitContext(
            project_name="my-agent",
            dev=_spec("https://wks.cloud.databricks.com", "shared"),
            prod=_spec("https://wks.cloud.databricks.com", "shared"),
            ci_provider="git",
            starter_template="blank",
        )


def test_init_context_validates_project_name() -> None:
    with pytest.raises(ValidationError, match="project_name"):
        InitContext(
            project_name="Has Spaces",
            dev=_spec("https://wks.cloud.databricks.com", "dev_cat"),
            ci_provider="git",
            starter_template="blank",
        )


def test_init_context_rejects_unknown_starter_template() -> None:
    with pytest.raises(ValidationError, match="starter_template"):
        InitContext(
            project_name="my-agent",
            dev=_spec("https://wks.cloud.databricks.com", "dev_cat"),
            ci_provider="git",
            starter_template="nonexistent-variant-xyz",
        )


def test_init_context_target_map_is_ordered() -> None:
    ctx = InitContext(
        project_name="my-agent",
        dev=_spec("https://wks.cloud.databricks.com", "dev_cat"),
        staging=_spec("https://wks.cloud.databricks.com", "staging_cat"),
        prod=_spec("https://wks.cloud.databricks.com", "prod_cat"),
        ci_provider="git",
        starter_template="blank",
    )
    assert list(ctx.target_map().keys()) == ["dev", "staging", "prod"]


def test_init_context_target_map_skips_optional_when_unset() -> None:
    ctx = InitContext(
        project_name="my-agent",
        dev=_spec("https://wks.cloud.databricks.com", "dev_cat"),
        ci_provider="git",
        starter_template="blank",
    )
    assert list(ctx.target_map().keys()) == ["dev"]


# ---------------------------------------------------------------------------
# collect() happy paths
# ---------------------------------------------------------------------------


def _happy_responses(
    *,
    name: str = "my-agent",
    dev_host: str = "https://wks.cloud.databricks.com",
    dev_catalog: str = "dev_cat",
    dev_profile: str = "",
    ci: CIProvider = "git",
    template: StarterTemplate = "blank",
) -> list[str]:
    """Minimal-required prompt answers for a dev-only happy path."""
    return [name, dev_host, dev_catalog, dev_profile, ci, template]


def test_collect_dev_only_blank_github() -> None:
    io = _ScriptedIO(
        responses=_happy_responses(),
        confirms=[False, False],
    )
    ctx = collect(io, framework_sha="abc123", framework_install_url="git+ssh://example.com")

    assert ctx.project_name == "my-agent"
    assert ctx.dev.catalog == "dev_cat"
    assert ctx.staging is None
    assert ctx.prod is None
    assert ctx.ci_provider == "git"
    assert ctx.starter_template == "blank"
    assert ctx.framework_sha == "abc123"


def test_collect_all_three_targets_full_sample_gitlab() -> None:
    io = _ScriptedIO(
        responses=[
            "portfolio-assistant",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "dev-profile",
            "",
            "staging_cat",
            "",
            "",
            "prod_cat",
            "",
            "gitlab",
            "portfolio_assistant",
        ],
        confirms=[True, True],
    )
    ctx = collect(io)

    assert ctx.project_name == "portfolio-assistant"
    assert ctx.dev.host == "https://wks.cloud.databricks.com"
    assert ctx.dev.profile == "dev-profile"
    assert ctx.staging is not None
    assert ctx.staging.host == "https://wks.cloud.databricks.com"
    assert ctx.prod is not None
    assert ctx.prod.catalog == "prod_cat"
    assert ctx.ci_provider == "gitlab"
    assert ctx.starter_template == "portfolio_assistant"


def test_collect_blank_template_prints_scaffold_hint() -> None:
    io = _ScriptedIO(
        responses=_happy_responses(template="blank"),
        confirms=[False, False],
    )
    collect(io)
    hint_lines = [line for line in io.echoes if "scaffold" in line]
    assert hint_lines, "blank template must print a scaffold hint"
    assert any("--list" in line for line in hint_lines)


def test_collect_variant_template_does_not_print_scaffold_hint() -> None:
    io = _ScriptedIO(
        responses=_happy_responses(template="portfolio_assistant"),
        confirms=[False, False],
    )
    collect(io)
    hint_lines = [line for line in io.echoes if "scaffold" in line.lower()]
    assert hint_lines == [], "variant template must not print the blank-only scaffold hint"


# ---------------------------------------------------------------------------
# collect() re-prompt loops
# ---------------------------------------------------------------------------


def test_collect_re_prompts_invalid_project_name() -> None:
    io = _ScriptedIO(
        responses=[
            "Has Spaces",  # invalid
            "also bad!",  # invalid
            "valid-name",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "git",
            "blank",
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.project_name == "valid-name"
    error_echoes = [e for e in io.echoes if "error" in e]
    assert len(error_echoes) == 2


def test_collect_re_prompts_invalid_host() -> None:
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "not-a-url",
            "ftp://wrong.com",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "git",
            "blank",
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.dev.host == "https://wks.cloud.databricks.com"
    error_echoes = [e for e in io.echoes if "error" in e and "host" in e]
    assert len(error_echoes) == 2


def test_collect_re_prompts_duplicate_catalog_on_staging() -> None:
    """If the user enters dev's catalog for staging, the wizard
    must re-prompt at the staging-catalog prompt -- not abort the
    whole wizard.
    """
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "shared",
            "",
            "",
            "shared",  # duplicate, must re-prompt
            "staging_cat",
            "",
            "git",
            "blank",
        ],
        confirms=[True, False],
    )
    ctx = collect(io)
    assert ctx.dev.catalog == "shared"
    assert ctx.staging is not None
    assert ctx.staging.catalog == "staging_cat"
    error_echoes = [e for e in io.echoes if "already used" in e]
    assert len(error_echoes) == 1


def test_collect_re_prompts_invalid_ci_provider() -> None:
    """Free-text fallback for choice prompts must still re-prompt
    on invalid values, not abort.  Sanity check for the
    ``_prompt_choice`` helper.
    """
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "github",
            "GitHub",
            "git",
            "blank",
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.ci_provider == "git"
    error_echoes = [e for e in io.echoes if "is not one of [git, gitlab]" in e]
    assert len(error_echoes) == 2


def test_collect_re_prompts_invalid_starter_template() -> None:
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "git",
            "minimal",
            "FULL",
            "portfolio_assistant",
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.starter_template == "portfolio_assistant"
    allowed = ", ".join(allowed_starter_templates())
    error_echoes = [e for e in io.echoes if f"is not one of [{allowed}]" in e]
    assert len(error_echoes) == 2


# ---------------------------------------------------------------------------
# Numeric-shortcut prompts (issue: init UX -- type a number instead of name)
# ---------------------------------------------------------------------------


def test_collect_accepts_numeric_ci_provider() -> None:
    """Users may type ``2`` instead of ``gitlab`` at the CI prompt.

    Numeric shortcuts cut the keystrokes-per-choice from up to six
    (``gitlab`` + Enter) down to two (``2`` + Enter) while keeping
    the name form working for scripted stdin / muscle memory.
    """
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "2",  # numeric shortcut for gitlab
            "blank",
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.ci_provider == "gitlab"


def test_collect_accepts_numeric_starter_template() -> None:
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "git",
            "2",  # numeric shortcut for portfolio_assistant
        ],
        confirms=[False, False],
    )
    ctx = collect(io)
    assert ctx.starter_template == "portfolio_assistant"


def test_collect_choice_menu_renders_numbered_options() -> None:
    """The CI and starter-template menus must surface the numeric
    indices in the output so the user discovers the shortcut.

    Without this assertion a future refactor could silently drop the
    numbers (e.g. by switching back to ``typer.Choice``) and the
    UX would regress to name-only input.
    """
    io = _ScriptedIO(
        responses=_happy_responses(),
        confirms=[False, False],
    )
    collect(io)

    assert any("1) git" in line for line in io.echoes), "CI provider menu must show '1) git'"
    assert any("2) gitlab" in line for line in io.echoes), "CI provider menu must show '2) gitlab'"
    assert any("1) blank" in line for line in io.echoes), "starter template menu must show '1) blank'"
    assert any("2) portfolio_assistant" in line for line in io.echoes), (
        "starter template menu must show '2) portfolio_assistant'"
    )
    assert any("Portfolio Assistant" in line for line in io.echoes), (
        "starter menu help text must include the variant display name"
    )


def test_collect_choice_prompt_label_hints_number_or_name() -> None:
    """The ``Choice (enter number or name)`` label tells the user both
    input forms work; Typer appends the ``[default]`` suffix itself.

    Pin the hint so a future refactor cannot silently drop it -- if
    the label collapses to bare ``Choice``, users who don't read the
    numbered option lines above would never discover the numeric
    shortcut.
    """
    captured_labels: list[str] = []
    captured_defaults: list[str | None] = []

    class _CapturingIO(_ScriptedIO):
        def prompt(self, message: str, default: str | None = None) -> str:
            captured_labels.append(message)
            captured_defaults.append(default)
            return super().prompt(message, default)

    io = _CapturingIO(
        responses=_happy_responses(),
        confirms=[False, False],
    )
    collect(io)

    choice_calls = [(lbl, dflt) for lbl, dflt in zip(captured_labels, captured_defaults) if lbl.startswith("Choice")]
    assert len(choice_calls) == 2, "expected 2 Choice prompts (CI + starter)"
    for label, _default in choice_calls:
        assert label == "Choice (enter number or name)"
    # Defaults still flow through ``io.prompt`` so Typer can render
    # ``[git]`` / ``[blank]`` in interactive mode.
    assert choice_calls[0][1] == "git"
    assert choice_calls[1][1] == "blank"


# ---------------------------------------------------------------------------
# Host-reuse default behavior
# ---------------------------------------------------------------------------


def test_collect_staging_reuses_dev_host_on_empty_input() -> None:
    """Empty answer at the host prompt for staging must fall back
    to dev's host (the default).
    """
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "",
            "",
            "staging_cat",  # empty host -> reuse dev's
            "",
            "git",
            "blank",
        ],
        confirms=[True, False],
    )
    ctx = collect(io)
    assert ctx.staging is not None
    assert ctx.staging.host == ctx.dev.host


def test_collect_staging_reuses_dev_profile_on_empty_input() -> None:
    io = _ScriptedIO(
        responses=[
            "my-agent",
            "https://wks.cloud.databricks.com",
            "dev_cat",
            "dev-profile",
            "",
            "staging_cat",
            "",  # reuse dev profile
            "git",
            "blank",
        ],
        confirms=[True, False],
    )

    ctx = collect(io)

    assert ctx.staging is not None
    assert ctx.staging.profile == "dev-profile"
