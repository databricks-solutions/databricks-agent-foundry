"""``agent-foundry init`` -- bootstrap a new downstream agent repo."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from agent_foundry._meta import DiscoveryResult


class _TyperInitIO:
    """Adapter from :class:`agent_foundry.init.wizard.InitIO` to Typer.

    Lives in the CLI module rather than the wizard package because
    importing ``typer`` from the wizard would force every consumer of
    :class:`InitContext` to pull in the full CLI dependency tree.
    The wizard stays test-friendly (scripted IO in unit tests); the
    CLI adapter is the only production caller.
    """

    def prompt(self, message: str, default: str | None = None) -> str:
        # ``typer.prompt`` is typed as ``Any`` in upstream stubs even
        # though our IO contract guarantees ``str``.
        if default is None:
            answer: str = typer.prompt(message)
            return answer
        answer = typer.prompt(message, default=default)
        return answer

    def confirm(self, message: str, default: bool = False) -> bool:
        return typer.confirm(message, default=default)

    def echo(self, message: str = "") -> None:
        typer.echo(message)


def init_cmd(
    project_name: str | None = typer.Option(
        None,
        "--name",
        help=("Project name (also the generated directory name). When omitted, the wizard prompts for it."),
    ),
    outdir: Path | None = typer.Option(
        None,
        "--outdir",
        help=(
            "Parent directory the new repo is created under. "
            "Defaults to the current working directory; the generated "
            "repo lands at ``<outdir>/<project_name>``."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite an existing target directory.  Without this "
            "flag, init refuses to write into a non-empty directory."
        ),
    ),
    framework_ref: str | None = typer.Option(
        None,
        "--framework-ref",
        help=(
            "Pin the generated repo's ``agent-foundry`` dependency to "
            "this git ref instead of the SHA discovered from the "
            "running CLI.  Useful for editable / local-path framework "
            "installs where SHA discovery cannot resolve a commit, "
            "and for explicitly choosing a different ref than the "
            "running install.  Any non-empty string is accepted; a "
            "warning is emitted when the value does not look like a "
            "git SHA (7-40 hex chars) to flag moving-target refs."
        ),
    ),
    no_lock: bool = typer.Option(
        False,
        "--no-lock",
        help=(
            "Skip generating ``uv.lock`` in the new repo.  By default "
            "init runs ``uv lock`` so the generated repo ships a "
            "committed lockfile -- every later ``uv sync`` / deploy / "
            "CI run then resolves from pinned wheels instead of a fresh "
            "resolution that depends on the package index serving PEP "
            "658 metadata correctly.  Use this for offline scaffolding "
            "and run ``uv lock`` manually before your first deploy."
        ),
    ),
    no_sync: bool = typer.Option(
        False,
        "--no-sync",
        help=(
            "Skip installing the new project's dependencies. By default "
            "init runs ``uv sync`` after a successful lock so the repo "
            "is ready to deploy. Use this for offline scaffolding."
        ),
    ),
) -> None:
    """Bootstrap a new downstream agent application repo.

    Interactively collects workspace targets (host + catalog for
    dev/staging/prod), a CI provider (GitHub Actions or GitLab CI),
    and a starter template (``blank`` or an ``examples/`` variant
    such as ``portfolio_assistant``).  Writes a generated repo
    skeleton outside the framework directory so users do not need to
    clone two repos to start a new agent.

    The generated repo pins the same ``agent-foundry`` SHA the
    running CLI was installed from when discoverable; otherwise it
    falls back to ``main`` with a warning.  Pass ``--framework-ref
    <sha>`` to override discovery -- required for editable / local-
    path framework installs (see #211).
    """
    from agent_foundry._meta import discover_framework_sha
    from agent_foundry.init import InitRenderError, collect, render

    discovery = discover_framework_sha()
    effective_sha = _resolve_framework_ref(framework_ref, discovery)
    install_url = _derive_install_url(discovery.requested_ref)

    io = _TyperInitIO()
    ctx = collect(
        io,
        framework_sha=effective_sha,
        framework_install_url=install_url,
    )

    if project_name and project_name != ctx.project_name:
        typer.echo(
            f"note: --name {project_name!r} overrides the wizard's collected project name {ctx.project_name!r}.",
        )
        ctx = ctx.model_copy(update={"project_name": project_name})

    target_root = (outdir or Path.cwd()).expanduser().resolve()
    target = target_root / ctx.project_name

    try:
        render(ctx, target, force=force)
    except InitRenderError as exc:
        typer.echo(f"init failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    locked = False if no_lock else _run_uv_lock(target)
    synced = False
    if locked and not no_sync:
        synced = _run_uv_sync(target)

    _echo_init_next_steps(
        target=target,
        target_root=target_root,
        host=ctx.dev.host,
        profile=ctx.dev.profile,
        locked=locked,
        synced=synced,
    )
    from agent_foundry.telemetry import emit_command

    emit_command("init")


def _run_uv_lock(target: Path) -> bool:
    """Generate ``uv.lock`` in the freshly-scaffolded repo.

    A committed lock makes every later ``uv sync`` / deploy / CI run
    resolve from pinned wheels (by URL + hash) instead of a fresh
    dependency resolution that depends on the package index serving
    PEP 658 ``.metadata`` sidecars correctly -- the failure mode that
    breaks lockless installs behind metadata-flaky index proxies.

    Missing ``uv`` is best-effort (warn and return ``False``). A
    present ``uv`` that fails to lock is a hard error: silently
    shipping a lockless repo lets downstreams float inside pyproject
    ranges and breaks the App constraint contract.
    """
    if shutil.which("uv") is None:
        typer.echo(
            "  note: 'uv' not found on PATH -- skipping lockfile generation. "
            "Run 'uv lock' in the generated repo before your first deploy.",
        )
        return False

    typer.echo("  Generating uv.lock (pins dependencies for reproducible deploys)...")
    proc = subprocess.run(
        ["uv", "lock"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        typer.echo(
            "  error: 'uv lock' failed -- the package index could not "
            "satisfy the generated pin. Fix the resolve (or pass "
            "--no-lock for offline scaffolding) before deploying.",
            err=True,
        )
        for line in (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]:
            typer.echo(f"    {line}", err=True)
        raise typer.Exit(code=2)

    return True


def _run_uv_sync(target: Path) -> bool:
    """Install the new project's dependencies into ``.venv``.

    Best-effort: a missing ``uv`` binary or an install failure warns
    and returns ``False`` rather than aborting, since the repo is
    already on disk. The user can run ``uv sync`` by hand.
    """
    if shutil.which("uv") is None:
        typer.echo(
            "  Could not install dependencies (uv not found). Run 'uv sync --frozen' in this folder.",
        )
        return False

    typer.echo("  Installing project dependencies (frozen lock)...")
    proc = subprocess.run(
        ["uv", "sync", "--frozen"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        typer.echo(
            "  Could not install dependencies; run 'uv sync --frozen' in this folder.",
            err=True,
        )
        last_line = proc.stderr.strip().splitlines()[-1:] if proc.stderr.strip() else []
        for line in last_line:
            typer.echo(f"    {line}", err=True)
        return False

    return True


def _echo_init_next_steps(
    *,
    target: Path,
    target_root: Path,
    host: str,
    profile: str | None,
    locked: bool,
    synced: bool,
) -> None:
    """Print post-init steps. Keep them short for first-time users."""
    typer.echo("")
    typer.echo(f"Done. Your repo is at {target}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {target.relative_to(target_root)}")
    typer.echo('  git init && git add . && git commit -m "Initial scaffold"')
    if not locked:
        typer.echo("  uv lock")
        typer.echo("  uv sync --frozen")
    elif not synced:
        typer.echo("  uv sync --frozen")
    profile_arg = f" --profile {profile}" if profile else ""
    typer.echo(f"  databricks auth login --host {host}{profile_arg}")
    typer.echo("  uv run agent-foundry deploy --target dev")


_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Canonical distributable source for the generated repo's ``agent-foundry``
# pin, used when the running install records no usable URL (or only a
# local ``file://`` path).  Downstream repos and CI must be able to fetch
# this, so it is a git remote rather than a filesystem path.
_CANONICAL_INSTALL_URL = "git+https://github.com/databricks-solutions/databricks-agent-foundry.git"


def _resolve_framework_ref(
    override: str | None,
    discovery: DiscoveryResult,
) -> str | None:
    """Decide which SHA / ref the generated repo's pyproject pins.

    Precedence (highest first):

    1. ``--framework-ref`` CLI override (warns if not SHA-shaped).
    2. SHA resolved by :func:`discover_framework_sha`.
    3. ``None`` -- renderer falls back to ``main`` with a stderr
       warning emitted here pointing the user at ``--framework-ref``.

    Returns the value to thread into :class:`InitContext.framework_sha`
    (``None`` keeps the renderer's ``main`` fallback).
    """
    if override is not None:
        cleaned = override.strip()
        if not cleaned:
            typer.echo(
                "error: --framework-ref must not be empty.",
                err=True,
            )
            raise typer.Exit(code=2)
        if not _GIT_SHA_RE.match(cleaned):
            typer.echo(
                f"warning: --framework-ref {cleaned!r} does not look "
                "like a git SHA (7-40 hex chars); the generated repo "
                "will be pinned to a moving target. Prefer a resolved "
                "commit SHA.",
                err=True,
            )
        if discovery.sha and discovery.sha != cleaned:
            typer.echo(
                f"note: --framework-ref {cleaned!r} overrides the discovered SHA {discovery.sha!r}.",
            )
        return cleaned

    if discovery.sha is None:
        typer.echo(
            "warning: could not discover the running agent-foundry "
            f"SHA ({discovery.reason}) The generated repo will pin "
            "``main`` -- rerun with ``--framework-ref <sha>`` to pin "
            "a specific commit, or edit pyproject.toml before the "
            "first deploy.",
            err=True,
        )
    return discovery.sha


def _derive_install_url(requested_ref: str) -> str:
    """Best-effort lookup of the framework's install URL.

    Reads ``direct_url.json`` directly here rather than extending
    :class:`DiscoveryResult` because the URL is only consumed by the
    renderer (PR3) and bloating the meta API for one caller seemed
    wrong.  Falls back to the canonical upstream URL on failure --
    the renderer further falls back to ``main`` if the SHA itself
    was unavailable.

    Editable / local-path installs are recorded in ``direct_url.json``
    as a ``file://`` URL.  Such a URL is unusable as the generated
    repo's dependency source: a downstream repo cannot depend on the
    maintainer's local checkout, and ``file://...@<sha>`` (the
    renderer appends the ref) is not even a valid spec.  When the
    running framework is a local install we therefore recover the
    checkout's ``origin`` remote so the generated pin is a
    distributable ``git+...`` source.
    """
    import json as _json
    from importlib import metadata as _metadata

    del requested_ref  # currently unused; reserved for future branch-ref UX

    try:
        dist = _metadata.distribution("agent-foundry")
        raw = dist.read_text("direct_url.json")
    except (_metadata.PackageNotFoundError, OSError):
        raw = None

    if raw:
        try:
            payload = _json.loads(raw)
        except _json.JSONDecodeError:
            payload = {}
        url = payload.get("url")
        is_local = bool(payload.get("dir_info")) or (isinstance(url, str) and url.startswith("file://"))
        if is_local and isinstance(url, str):
            return _git_remote_install_url(url) or _CANONICAL_INSTALL_URL
        if isinstance(url, str) and url:
            return url

    return _CANONICAL_INSTALL_URL


def _git_remote_install_url(file_url: str) -> str | None:
    """Resolve a checkout's ``origin`` remote as a ``git+...`` source.

    Maps the ``file://`` URL recorded for an editable / local install
    back to a filesystem path, reads ``remote.origin.url`` from that
    checkout, and normalises it to the ``git+<scheme>://`` form pip /
    uv require inside ``pyproject.toml`` dependencies.  Returns
    ``None`` when the path is gone, git is unavailable, or no origin
    is configured so the caller can fall back to the canonical URL.
    """
    from urllib.parse import unquote, urlparse

    local = Path(unquote(urlparse(file_url).path))
    if not local.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(local), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _normalize_git_remote(proc.stdout)


def _normalize_git_remote(remote: str) -> str | None:
    """Normalise a git remote URL to a ``git+...`` pip/uv source.

    Handles the three forms ``git remote`` emits: an explicit scheme
    (``ssh://`` / ``https://`` / ``git://``), the scp-like shorthand
    (``git@host:org/repo.git``), and an already-prefixed ``git+...``
    URL.  Returns ``None`` for anything unrecognised (e.g. a local
    path remote) so the caller falls back to the canonical URL.
    """
    remote = remote.strip()
    if not remote:
        return None
    if remote.startswith(("git+", "hg+", "bzr+", "svn+")):
        return remote
    if remote.startswith(("ssh://", "https://", "http://", "git://")):
        return f"git+{remote}"
    scp = re.match(r"^([^@/]+)@([^:/]+):(.+)$", remote)
    if scp:
        user, host, path = scp.groups()
        return f"git+ssh://{user}@{host}/{path}"
    return None


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("init")(init_cmd)
