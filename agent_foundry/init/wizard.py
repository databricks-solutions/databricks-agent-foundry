"""Interactive wizard for ``agent-foundry init`` (issue #210).

Responsibilities:

* Prompt the user for the inputs the renderer (PR3) needs: project
  name, workspace target(s), VCS provider, starter template.
* Validate inputs at authoring time (strict catalog uniqueness, host
  format, name format) and re-prompt on failure rather than aborting
  the wizard.
* Hand a fully-validated :class:`InitContext` to the caller.

This module contains **no file IO**.  The wizard is pure
orchestration: prompts in, validated model out.  PR3's renderer
takes the model and writes the generated repo to disk; PR4 wires
the CLI verb.

Testing strategy
----------------

:func:`collect` accepts an :class:`InitIO` protocol so unit tests
can drive the wizard with scripted prompt responses instead of
real stdin.  In production the CLI passes an :class:`InitIO`
backed by ``typer.prompt`` / ``typer.confirm`` / ``typer.echo``.
"""

from __future__ import annotations

import re
from typing import Callable, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from agent_foundry.init.examples_registry import (
    ExamplesRegistryError,
    allowed_starter_templates,
    get_variant,
    list_starter_options,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

CIProvider = Literal["git", "gitlab"]
"""VCS / CI provider choice.  ``git`` writes GitHub Actions workflows;
``gitlab`` writes a GitLab CI configuration.  Exactly one is written
per generated repo (see #210 -- both-always was rejected in favor of
wizard-prompted single-provider output).
"""

StarterTemplate = str
"""Starter content for ``instances/<name>/``.

* ``blank``: identity-only scaffold.  After init the wizard prints
  a hint pointing at ``agent-foundry scaffold <brick>`` so the user
  can add bricks incrementally.
* Any other value is an ``examples/<variant>/`` directory id
  (e.g. ``portfolio_assistant``) discovered from ``manifest.toml``
  files via :func:`agent_foundry.init.examples_registry.list_variants`.
  Adding a new end-to-end example does not require wizard code changes.
"""

_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
# Accepts AWS/GCP (``*.databricks.com``) and Azure (``*.azuredatabricks.net``)
# workspace URLs. Azure Databricks does not use the ``.databricks.com`` suffix.
_HOST_RE = re.compile(r"^https://[^\s/]+\.(?:databricks\.com|azuredatabricks\.net)/?$")
# Unity Catalog allows hyphens in catalog names (e.g. ``CREATE CATALOG
# `cat-a-log```); only ``.``, space, ``/`` and control chars are forbidden.
# Such names must be backtick-quoted when referenced in SQL.
_CATALOG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class TargetSpec(BaseModel):
    """One workspace target collected by the wizard.

    Mirrors the on-disk shape of
    :class:`agent_foundry.public.foundry_config.Target` but carries
    the workspace location, catalog, and optional local CLI profile.
    Other ``Target`` fields (``deployer``, ``schema``) keep their
    model defaults at generation time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    catalog: str
    profile: str | None = None

    @field_validator("host", mode="after")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if not _HOST_RE.match(value):
            raise ValueError(
                f"host {value!r} does not look like a Databricks workspace URL "
                "(expected e.g. https://dbc-xxx.cloud.databricks.com or "
                "https://adb-xxx.azuredatabricks.net)"
            )
        return value.rstrip("/")

    @field_validator("catalog", mode="after")
    @classmethod
    def _validate_catalog(cls, value: str) -> str:
        if not _CATALOG_RE.match(value):
            raise ValueError(
                f"catalog {value!r} must be a valid Unity Catalog identifier "
                "(lowercase letters, digits, underscores, hyphens; must start with a letter)"
            )
        return value

    @field_validator("profile", mode="after")
    @classmethod
    def _validate_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _PROFILE_RE.match(cleaned):
            raise ValueError("profile may contain only letters, digits, dots, underscores, and hyphens")
        return cleaned


class InitContext(BaseModel):
    """Validated bundle of all wizard outputs.

    The renderer (PR3) consumes this model and writes the generated
    repo; no other state crosses the wizard / renderer boundary.
    Catalog uniqueness across targets is enforced strictly here --
    the runtime warning on :class:`FoundryRepoConfig` is a safety
    net for hand-edits, not a substitute for authoring-time
    validation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_name: str
    dev: TargetSpec
    staging: TargetSpec | None = None
    prod: TargetSpec | None = None
    ci_provider: CIProvider
    starter_template: StarterTemplate
    framework_sha: str | None = None
    framework_install_url: str = ""

    @field_validator("project_name", mode="after")
    @classmethod
    def _validate_project_name(cls, value: str) -> str:
        if not _PROJECT_NAME_RE.match(value):
            raise ValueError(
                f"project_name {value!r} must be lowercase alphanumeric with "
                "optional internal hyphens (e.g. 'portfolio-assistant')"
            )
        return value

    @field_validator("starter_template", mode="after")
    @classmethod
    def _validate_starter_template(cls, value: str) -> str:
        if value == "blank":
            return value
        try:
            get_variant(value)
        except ExamplesRegistryError as exc:
            allowed = ", ".join(allowed_starter_templates())
            raise ValueError(
                f"starter_template {value!r} is not a known variant (expected one of: {allowed}): {exc}"
            ) from exc
        return value

    @model_validator(mode="after")
    def _catalogs_must_be_unique(self) -> "InitContext":
        catalogs: list[tuple[str, str]] = [("dev", self.dev.catalog)]
        if self.staging is not None:
            catalogs.append(("staging", self.staging.catalog))
        if self.prod is not None:
            catalogs.append(("prod", self.prod.catalog))

        seen: dict[str, list[str]] = {}
        for name, catalog in catalogs:
            seen.setdefault(catalog, []).append(name)
        duplicates = {c: ns for c, ns in seen.items() if len(ns) > 1}
        if duplicates:
            details = ", ".join(f"{c!r} on [{', '.join(ns)}]" for c, ns in sorted(duplicates.items()))
            raise ValueError(
                "catalogs must be unique across targets (same host is fine, "
                f"same catalog defeats environment isolation): {details}"
            )
        return self

    def target_map(self) -> dict[str, TargetSpec]:
        """Return a dict of all configured targets keyed by name.

        Convenience for the renderer (PR3) so it does not have to
        re-implement the optional-target collation logic.  Order is
        deterministic (dev / staging / prod) so the generated
        ``foundry.toml`` block ordering matches the wizard's prompt
        ordering.
        """
        targets: dict[str, TargetSpec] = {"dev": self.dev}
        if self.staging is not None:
            targets["staging"] = self.staging
        if self.prod is not None:
            targets["prod"] = self.prod
        return targets


# ---------------------------------------------------------------------------
# IO protocol (lets tests drive prompts without real stdin)
# ---------------------------------------------------------------------------


class InitIO(Protocol):
    """Minimal IO surface the wizard needs.

    Production callers (PR4's CLI verb) supply an adapter backed by
    ``typer.prompt`` / ``typer.confirm`` / ``typer.echo``.  Unit
    tests supply a scripted adapter that replays canned answers.

    Methods deliberately mirror the ``typer`` primitives so the
    adapter is a thin pass-through with no behavior of its own.
    """

    def prompt(self, message: str, default: str | None = None) -> str: ...
    def confirm(self, message: str, default: bool = False) -> bool: ...
    def echo(self, message: str = "") -> None: ...


# ---------------------------------------------------------------------------
# Prompt loop
# ---------------------------------------------------------------------------


def collect(
    io: InitIO,
    *,
    framework_sha: str | None = None,
    framework_install_url: str = "",
) -> InitContext:
    """Drive the interactive wizard against ``io`` and return the
    validated context.

    Arguments:
        io: IO adapter for prompts / output.
        framework_sha: Resolved SHA the running CLI was installed
            from (passed through to the renderer for the
            ``pyproject.toml`` pin).  ``None`` when discovery
            failed; the renderer falls back to writing ``main``.
        framework_install_url: Full git+ssh URL the framework was
            installed from, used by the renderer to construct the
            generated repo's framework pin.  Empty string when
            unknown.

    The function re-prompts on validation errors rather than
    aborting, so a typo in a host or catalog does not force the
    user to restart the wizard.  The only way to exit early is
    KeyboardInterrupt at a prompt, which propagates to the caller.
    """
    io.echo("agent-foundry init -- bootstrap a new agent application repo")
    io.echo("")

    project_name = _prompt_until_valid(
        io,
        message="Project name (lowercase, hyphenated)",
        default="my-agent",
        parse=lambda v: v.strip(),
        validate=_validate_project_name_input,
    )

    io.echo("")
    io.echo("-- Workspace targets --")
    io.echo("A 'target' is one Databricks workspace + catalog combination.")
    io.echo("You need at least 'dev'. 'staging' and 'prod' are optional.")
    io.echo("Hosts may be reused across targets; catalogs must be unique.")
    io.echo("")

    dev = _prompt_target(io, target_name="dev", default_host=None, default_profile=None)
    staging: TargetSpec | None = None
    prod: TargetSpec | None = None

    if io.confirm("Add a staging target?", False):
        staging = _prompt_target(
            io,
            target_name="staging",
            default_host=dev.host,
            default_profile=dev.profile,
            existing_catalogs=[dev.catalog],
        )
    if io.confirm("Add a prod target?", False):
        existing = [dev.catalog]
        if staging is not None:
            existing.append(staging.catalog)
        prod = _prompt_target(
            io,
            target_name="prod",
            default_host=dev.host,
            default_profile=dev.profile,
            existing_catalogs=existing,
        )

    io.echo("")
    io.echo("-- Version control / CI --")
    ci_provider = _prompt_ci_provider(io)

    io.echo("")
    io.echo("-- Starter content --")
    starter_template = _prompt_starter_template(io)

    # Final model_validator catches any cross-field issues that the
    # per-prompt validators could not (e.g. a future field whose
    # validity depends on multiple inputs).
    try:
        ctx = InitContext(
            project_name=project_name,
            dev=dev,
            staging=staging,
            prod=prod,
            ci_provider=ci_provider,
            starter_template=starter_template,
            framework_sha=framework_sha,
            framework_install_url=framework_install_url,
        )
    except ValidationError as exc:
        # This branch should be unreachable in practice -- every per-
        # field validator above is called from inside the prompt loop
        # so the user has already had a chance to fix the input.  If
        # it does fire (e.g. a future cross-field invariant) we surface
        # the pydantic message verbatim.
        raise RuntimeError(f"wizard input failed final validation: {exc}") from exc

    io.echo("")
    io.echo(f"Will create ./{project_name}/ with the inputs you provided.")
    if starter_template == "blank":
        io.echo("")
        io.echo(
            "Tip: add bricks later with `agent-foundry scaffold <brick>`. "
            "Run `agent-foundry scaffold --list` to see what's available.",
        )
    return ctx


# ---------------------------------------------------------------------------
# Per-prompt helpers (kept top-level: no nested function defs)
# ---------------------------------------------------------------------------


def _prompt_target(
    io: InitIO,
    *,
    target_name: str,
    default_host: str | None,
    default_profile: str | None,
    existing_catalogs: list[str] | None = None,
) -> TargetSpec:
    """Collect one ``TargetSpec`` with host, catalog, and CLI profile."""
    io.echo(f"{target_name}:")
    host_default = default_host
    host_prompt = "  host" if host_default is None else f"  host [enter to reuse {default_host}]"

    host = _prompt_until_valid(
        io,
        message=host_prompt,
        default=host_default,
        parse=lambda v: v.strip(),
        validate=_validate_host_input,
    )

    existing = existing_catalogs or []
    catalog = _prompt_until_valid(
        io,
        message="  catalog",
        default=None,
        parse=lambda v: v.strip(),
        validate=lambda v: _validate_catalog_input(v, existing=existing),
    )

    reusable_profile = default_profile if host == default_host else None
    profile_prompt = "  Databricks CLI profile (optional)"
    if reusable_profile is not None:
        profile_prompt = f"  Databricks CLI profile [enter to reuse {reusable_profile}]"
    profile = _prompt_until_valid(
        io,
        message=profile_prompt,
        # Typer does not accept an empty answer when no default is supplied.
        # An empty-string default makes this genuinely optional.
        default=reusable_profile or "",
        parse=lambda v: v.strip(),
        validate=_validate_profile_input,
    )

    return TargetSpec(host=host, catalog=catalog, profile=profile or None)


def _prompt_ci_provider(io: InitIO) -> CIProvider:
    """Prompt for the CI provider as a numbered choice.

    The user may type the number ``1`` / ``2`` or the literal name
    (``git`` / ``gitlab``); both forms are accepted to preserve
    scripted-input compatibility (CliRunner tests, copy-pasted
    walkthroughs) while still offering a single-keystroke path for
    interactive use.
    """
    io.echo("Which CI provider?")
    # ``_prompt_choice`` returns ``str`` but is constrained by the
    # ``options`` list to exactly the ``CIProvider`` literals; cast at
    # the boundary so the public function's narrow return type stays.
    return cast(
        CIProvider,
        _prompt_choice(
            io,
            options=[
                ("git", "GitHub Actions"),
                ("gitlab", "GitLab CI"),
            ],
            default="git",
        ),
    )


def _prompt_starter_template(io: InitIO) -> StarterTemplate:
    """Prompt for the starter template as a numbered choice.

    Options are built from :func:`list_starter_options` so new
    ``examples/<variant>/`` trees appear in the menu automatically
    when they ship a ``manifest.toml``.
    """
    io.echo("How much do you want pre-populated?")
    return _prompt_choice(
        io,
        options=list_starter_options(),
        default="blank",
    )


def _prompt_choice(
    io: InitIO,
    *,
    options: list[tuple[str, str]],
    default: str,
) -> str:  # noqa: D401 -- imperative docstring fits a UI helper
    """Render a numbered menu and accept the number OR the literal name.

    ``options`` is an ordered list of ``(value, help_text)`` pairs;
    each is rendered as ``  N) value  -- help_text``.  Input is
    matched case-insensitively against both the 1-based index and
    the literal value.  Re-prompts on invalid input rather than
    aborting -- mirrors the rest of the wizard.

    The prompt label embeds the default in ``[N/value]`` form so the
    rendered output is self-documenting:

        Choice [1/git]:

    Accepting the literal name keeps the wizard compatible with
    scripted stdin (existing CliRunner tests, copy-pasted
    walkthroughs) and with users who learned the name forms from
    earlier docs.
    """
    if default not in {value for value, _ in options}:
        # Programming error in the caller, not a user issue.
        raise ValueError(f"default {default!r} is not one of the option values {[value for value, _ in options]!r}")

    width = max(len(value) for value, _ in options)
    for index, (value, help_text) in enumerate(options, start=1):
        io.echo(f"  {index}) {value.ljust(width)}  -- {help_text}")

    # The label keeps Typer's standard ``[default]`` suffix (Typer
    # appends it automatically when ``default`` is non-None); the
    # numbered option lines printed above carry the shortcut hint so
    # we do not duplicate the default in the label itself.  See the
    # post-rendering check
    # ``test_collect_choice_menu_renders_numbered_options`` for the
    # contract this relies on.
    label = "Choice (enter number or name)"

    valid_by_index = {str(i): value for i, (value, _) in enumerate(options, start=1)}
    valid_by_name = {value.lower(): value for value, _ in options}

    while True:
        raw = io.prompt(label, default).strip()
        if not raw:
            # ``typer.prompt`` already substitutes the default for
            # empty input, but the scripted-IO test double does so
            # only when ``default`` is non-None -- mirror that here
            # so behavior is consistent across adapters.
            return default
        key = raw.lower()

        if key in valid_by_index:
            return valid_by_index[key]
        if key in valid_by_name:
            return valid_by_name[key]

        names = ", ".join(value for value, _ in options)
        io.echo(f"  error: {raw!r} is not one of [{names}] (or 1..{len(options)}). Try again.")


def _prompt_until_valid(
    io: InitIO,
    *,
    message: str,
    default: str | None,
    parse: Callable[[str], str],
    validate: Callable[[str], str | None],
) -> str:
    """Generic re-prompt loop.

    ``validate`` returns ``None`` on success or an error message
    string on failure.  Returning a string rather than raising lets
    each per-field validator emit its own user-friendly message
    without exception machinery in the prompt loop.
    """
    while True:
        raw = io.prompt(message, default) if default is not None else io.prompt(message)
        value = parse(raw)
        error = validate(value)
        if error is None:
            return value
        io.echo(f"  error: {error}")


def _validate_project_name_input(value: str) -> str | None:
    if not value:
        return "project name cannot be empty"
    if not _PROJECT_NAME_RE.match(value):
        return "project name must be lowercase alphanumeric with optional internal hyphens (e.g. 'portfolio-assistant')"
    return None


def _validate_host_input(value: str) -> str | None:
    if not value:
        return "host cannot be empty"
    if not _HOST_RE.match(value):
        return (
            "host must look like https://dbc-xxx.cloud.databricks.com or "
            "https://adb-xxx.azuredatabricks.net (missing scheme, wrong domain, or trailing path)"
        )
    return None


def _validate_catalog_input(value: str, *, existing: list[str]) -> str | None:
    if not value:
        return "catalog cannot be empty"
    if not _CATALOG_RE.match(value):
        return (
            "catalog must be a valid Unity Catalog identifier "
            "(lowercase letters, digits, underscores, hyphens; must start with a letter)"
        )
    if value in existing:
        return (
            f"catalog {value!r} is already used by another target. "
            "Catalogs must be unique across dev/staging/prod -- "
            "same host is fine, same catalog defeats environment isolation."
        )
    return None


def _validate_profile_input(value: str) -> str | None:
    if value and not _PROFILE_RE.match(value):
        return "profile may contain only letters, digits, dots, underscores, and hyphens"
    return None


__all__ = [
    "CIProvider",
    "InitContext",
    "InitIO",
    "StarterTemplate",
    "TargetSpec",
    "collect",
]
