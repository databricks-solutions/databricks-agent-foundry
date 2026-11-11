"""
Wheel-based deploy staging
==========================
Build a self-contained payload for ``databricks sync`` so the deployed
Databricks App pulls runtime code from a pip-installed wheel rather than
from synced source files (issue #112).

Layout produced by :func:`build_deploy_staging`::

    <staging>/
    ├── agent_foundry-<version>-py3-none-any.whl
    ├── requirements.txt        # ./<wheel>[extras] plus -c constraint files
    ├── constraints-runtime.txt # lock-derived pins for the selected extras
    ├── README.md               # optional payload README
    └── instances/<name>/       # copied from local instance dir

The caller (a brick deploy script) syncs the entire staging dir to the
workspace.  The App container then runs ``pip install -r
requirements.txt`` and the wheel pulls the extras selected by the payload
(``runtime`` for agent Apps, ``rest_mcp`` for REST-to-MCP proxies) -- no
source files are required at runtime.

Replaces the interim ``_stage_for_sync`` helper in
``bricks/app/scripts/deploy_agent_app.py``.

Usage::

    handle = build_deploy_staging(instance_dir)
    try:
        subprocess.run(["databricks", "sync", handle.name, ws_path, ...])
    finally:
        handle.cleanup()

Implicit invariant
------------------
``build_deploy_staging`` requires ``agent-foundry`` to be importable
(``importlib.metadata.distribution("agent-foundry")`` must resolve).
This is always true when the helper is called via ``agent-foundry
deploy`` because the CLI itself ships in the ``agent-foundry``
distribution.  Documented here so a future reader who tries to call
this helper from a context without ``agent-foundry`` on the path
sees the contract.

Trade-off space (for future maintainers)
----------------------------------------
The wheel re-pack from PEP 376 ``RECORD`` is a deliberate side-step
of the standard "publish to a registry, install by version" pattern.
It exists because:

* No internal PyPI / UC-Volume publish pipeline today (out of scope
  per issue #112).
* uv-managed venvs don't seed ``pip``, so we cannot shell out to
  ``pip wheel agent-foundry --no-deps`` -- that would otherwise be
  the one-line equivalent of :func:`_pack_installed_framework_wheel`.
* Pinning ``agent-foundry @ git+ssh://...`` in the generated
  ``requirements.txt`` would require git+ssh credentials in the App
  container (issue #112's open question 1(a)).

When any of those constraints relaxes, the staging helper should
collapse to a tiny ``requirements.txt`` writer pointing at the
published wheel -- the rest of the deploy plumbing stays as-is.

Wheel sourcing
--------------
The shipped wheel is sourced from the **currently-installed**
``agent-foundry`` distribution (see :func:`_pack_installed_framework_wheel`).

Two strategies, picked automatically based on the install kind:

* **Standard install** (downstream consumer, ``pip install
  agent-foundry @ git+ssh://...`` or PyPI): the framework's package
  files are concrete ``site-packages/agent_foundry/`` and
  ``site-packages/bricks/`` trees, listed in the distribution's PEP 376
  ``RECORD``.  Re-pack them into a fresh ``.whl`` by zipping -- no
  build tools required.

* **Editable install** (framework dev workflow, ``pip install -e .``
  / ``uv sync`` from the framework repo): ``RECORD`` is either
  missing (uv) or lists only an ``__editable__.*.pth`` finder shim,
  so site-packages does not have the source.  Fall back to
  ``python -m build --wheel`` against the framework source tree
  (located via ``agent_foundry.__file__``).

The earlier (broken) design called ``python -m build`` against
``project_root=Path.cwd()`` unconditionally -- which silently built the
**downstream** repo's wheel when invoked from a downstream repo (no
``runtime`` extra, no framework code), and the App container then
crashed at startup.  That code path is gone.

Notes
-----
* The version segment of the wheel filename comes from the installed
  distribution's ``METADATA`` (typically ``0.1.0``).  Local-version
  stamping (e.g. ``0.1.0+<git_sha>``) is intentionally NOT done here.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib.metadata import distribution
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

# Patterns excluded when copying the instance dir into the staging dir.
# ``_build/`` artifacts are not needed by the deployed App, and a leftover
# pre-workspace-state ``_state/`` dir may hold resource IDs; neither is synced.
_INSTANCE_COPY_EXCLUDES: tuple[str, ...] = (
    "_state",
    "_build",
    "__pycache__",
    "*.pyc",
    ".DS_Store",
)


def build_deploy_staging(
    instance_dir: Path,
    *,
    extras: tuple[str, ...] = ("runtime",),
    readme: Path | None = None,
) -> tempfile.TemporaryDirectory[str]:
    """Build a wheel-based deploy staging dir.

    Returns a :class:`tempfile.TemporaryDirectory` handle; the caller MUST
    keep it alive until the ``databricks sync`` subprocess exits, then
    call ``.cleanup()`` (idiomatically via ``try``/``finally`` or
    ``with``).

    Parameters
    ----------
    instance_dir
        Path to the application instance directory (the dir containing
        ``foundry.toml``).  Copied wholesale into
        ``<staging>/instances/<name>/``.
    extras
        Wheel extras to install in the App container, written into the
        generated ``requirements.txt`` as ``./<wheel>[<extras>]``.
        Defaults to ``("runtime",)``; the Agent App brick appends
        ``"extraction"`` when the file-upload toggle is on so the
        out-of-process extractor's deps (fastapi/uvicorn/openpyxl/...)
        land in the container.  Lock-derived ``constraints-<extra>.txt``
        files are always copied and referenced with ``-c``.
    readme
        Optional payload README copied to ``<staging>/README.md`` so it
        sits at the root of the App's source code.

    Raises
    ------
    FileNotFoundError
        If *instance_dir* does not exist.
    RuntimeError
        If the installed ``agent-foundry`` distribution cannot be
        located or its ``RECORD`` is malformed.

    See also
    --------
    The module docstring for the implicit ``agent-foundry`` install
    invariant and the editable-install fallback path.
    """
    instance_dir = instance_dir.resolve()
    if not instance_dir.exists():
        raise FileNotFoundError(f"instance_dir={instance_dir} does not exist")

    handle = tempfile.TemporaryDirectory(prefix="agent-foundry-deploy-")
    staging = Path(handle.name)

    try:
        wheel_path = _pack_installed_framework_wheel(staging)
        wheel_path = _stamp_wheel_version(wheel_path, _make_local_version_segment())
        _write_requirements(staging, wheel_path.name, extras=extras)
        _copy_instance(instance_dir, staging)
        if readme is not None:
            shutil.copyfile(readme, staging / "README.md")
    except Exception:
        handle.cleanup()
        raise

    return handle


def _pack_installed_framework_wheel(staging: Path) -> Path:
    """Produce an ``agent-foundry`` wheel in *staging*.

    Two strategies, picked automatically:

    * **Standard install** (downstream consumer): the framework's
      package files live under ``site-packages``, listed in PEP 376
      ``RECORD``.  Re-pack them into a wheel by zipping; the resulting
      wheel carries the same ``METADATA`` (incl. the ``[runtime]``
      extra) and the same package data
      (``payloads/agent/ui/overrides/**/*`` etc.) as a freshly-built
      wheel.  No build tools required.

    * **Editable / uv-sync install** (framework dev workflow):
      ``RECORD`` is missing, or lists only an ``__editable__.*.pth``
      finder shim.  ``site-packages`` does not have the source.
      Fall back to ``python -m build --wheel`` against the source
      tree (located via ``agent_foundry.__file__``).

    Why not ``python -m build`` from the deploy CWD?  In a downstream
    consumer's venv there is no framework source tree on disk, and
    walking up from CWD lands in the **downstream** repo's
    ``pyproject.toml`` (the wrong package, with no ``runtime`` extra).
    The earlier implementation made that mistake; this design avoids
    it entirely by anchoring on the installed distribution.
    """
    dist = distribution("agent-foundry")
    # ``Distribution.locate_file`` is typed as ``SimplePath`` (a
    # protocol), not ``os.PathLike``; the runtime object is
    # ``pathlib.Path``-compatible but the stub doesn't promise that.
    # ``str(...)`` is the documented portable narrowing.
    site_packages = Path(str(dist.locate_file(""))).resolve()

    record_text = dist.read_text("RECORD")
    # uv's editable / ``uv sync`` installs often omit PEP 376 RECORD
    # entirely (issue #469).  Same situation as a RECORD that only
    # lists the ``__editable__.*.pth`` shim: site-packages does not
    # hold the source, so re-pack from RECORD is impossible.  Fall
    # back to ``python -m build`` against the framework tree.
    if not record_text or _is_editable_install(record_text):
        return _build_wheel_from_source(staging)

    wheel_name = f"agent_foundry-{dist.version}-py3-none-any.whl"
    out = staging / wheel_name

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for line in record_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # RECORD format: <path>,<sha256_b64>,<size>
            rel = line.split(",", 1)[0]
            # Skip console-script entry-point shims that pip generates
            # under ``../../../bin/<name>``; the install side regenerates
            # them from ``entry_points.txt`` when the wheel is installed.
            if rel.startswith("../"):
                continue
            src = site_packages / rel
            if not src.exists():
                continue
            zf.write(src, arcname=rel)

    return out


def _is_editable_install(record_text: str) -> bool:
    """True if the RECORD points at an ``__editable__.*.pth`` shim
    rather than at concrete package files.

    pip / setuptools writes a single ``__editable__.<dist>-<ver>.pth``
    line (and an optional finder ``.py``) into ``site-packages`` for
    PEP 660 editable installs.  No package source files are present.
    """
    for line in record_text.splitlines():
        rel = line.split(",", 1)[0]
        if rel.startswith("__editable__"):
            return True
    return False


def _build_wheel_from_source(staging: Path) -> Path:
    """Fall back for editable installs: ``python -m build --wheel``
    against the framework source tree (located via
    ``agent_foundry.__file__``).

    Editable installs leave ``agent_foundry.__file__`` pointing at the
    source tree (the ``.pth`` finder rewrites import resolution back to
    the original repo).  Walking up to the nearest ``pyproject.toml``
    lands at the framework repo root, which is exactly what ``build``
    needs.

    Requires ``build`` to be importable -- it is in
    ``[project.optional-dependencies].dev``, which the framework dev
    workflow always installs.
    """
    import agent_foundry  # noqa: PLC0415

    framework_init = Path(agent_foundry.__file__).resolve()
    framework_root = None
    for cand in framework_init.parents:
        if (cand / "pyproject.toml").exists():
            framework_root = cand
            break
    if framework_root is None:
        raise RuntimeError(
            "agent-foundry is installed editable but no `pyproject.toml` "
            f"was found above {framework_init}; cannot build wheel for deploy"
        )

    cmd = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--outdir",
        str(staging),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(framework_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Isolated pep517 installs setuptools from the default index.
        # Hardened CI (PyPI / blocked PyPI) cannot do that; reuse the
        # already-installed ``build`` + ``setuptools`` from [dev].
        isolated_err = result
        cmd.append("--no-isolation")
        result = subprocess.run(
            cmd,
            cwd=str(framework_root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"`python -m build` failed in {framework_root}:\n"
                f"  isolated stdout: {isolated_err.stdout}\n"
                f"  isolated stderr: {isolated_err.stderr}\n"
                f"  no-isolation stdout: {result.stdout}\n"
                f"  no-isolation stderr: {result.stderr}"
            )

    wheels = sorted(staging.glob("agent_foundry-*.whl"))
    if not wheels:
        raise RuntimeError(
            f"`python -m build` produced no wheel in {staging}; got: {[p.name for p in staging.iterdir()]}"
        )
    return wheels[-1]


def _make_local_version_segment() -> str:
    """Return a per-deploy unique PEP 440 local version segment.

    Used by :func:`_stamp_wheel_version` to make pip see every deploy's
    wheel as a fresh upgrade (issue #185).  ``int(time.time())`` is
    chosen for simplicity and monotonicity across deploys; the segment
    ends up looking like ``"1747068123"`` and PEP 440 accepts it
    verbatim (``[a-zA-Z0-9.]``).  No subprocess required, no git
    repo dependency.
    """
    return str(int(time.time()))


def _stamp_wheel_version(wheel_path: Path, stamp: str) -> Path:
    """Rewrite *wheel_path*'s METADATA, dist-info dir name, and RECORD
    so the installed version becomes ``<orig_version>+<stamp>``
    (a PEP 440 local version segment).  Returns the path to the new
    stamped wheel.

    Why this exists (issue #185)
    ----------------------------
    ``build_deploy_staging`` always emits a wheel filename of
    ``agent_foundry-0.1.0-py3-none-any.whl`` (same name + same internal
    METADATA ``Version: 0.1.0`` on every deploy).  Databricks Apps
    reuses the App container's venv across redeploys, so when pip sees
    ``Version: 0.1.0`` already installed it short-circuits with
    "Requirement already satisfied" and the new wheel's contents never
    land in ``site-packages``.  After a refactor that renames or moves
    package files (e.g. #151's ``bricks/agent`` -> ``bricks/
    agent_runtime``), the App crashes at startup on the missing module.

    Stamping the wheel with a unique local version segment per deploy
    forces pip to see a fresh upgrade target every time, while keeping
    the public version (``0.1.0``) stable for downstream consumers.
    Local version segments are stripped by default for upstream
    requirement matching (PEP 440 §local-version), so a downstream
    ``agent-foundry==0.1.0`` constraint still resolves cleanly.

    What we rewrite
    ---------------
    Inside the wheel zip:
      * ``<dist>-<old>.dist-info/METADATA`` -- ``Version:`` line.
      * ``<dist>-<old>.dist-info/`` -- renamed to ``<dist>-<new>.dist-info/``.
      * ``<dist>-<new>.dist-info/RECORD`` -- regenerated with the new
        dist-info paths and a fresh ``sha256=...`` + size for METADATA.
        ``RECORD`` itself stays as the empty-hash row per PEP 376.
      * Wheel filename on disk is renamed to
        ``<dist>-<new>-py3-none-any.whl``.

    All non-dist-info entries (package files, package-data) are copied
    byte-for-byte; their RECORD rows only have their dist-info path
    prefix updated.

    Raises
    ------
    RuntimeError
        If the wheel does not contain a single matching
        ``*.dist-info/METADATA`` or its ``Version:`` line is missing.
    """
    with zipfile.ZipFile(wheel_path, "r") as zf_in:
        names = zf_in.namelist()
        old_dist_info = _find_dist_info_dir(names)
        old_meta_text = zf_in.read(f"{old_dist_info}/METADATA").decode("utf-8")
        old_record_text = zf_in.read(f"{old_dist_info}/RECORD").decode("utf-8")
        old_version = _extract_version(old_meta_text)
        new_version = f"{old_version}+{stamp}"
        new_dist_info = old_dist_info.replace(
            f"-{old_version}.dist-info",
            f"-{new_version}.dist-info",
        )

        new_meta_text = old_meta_text.replace(
            f"Version: {old_version}\n",
            f"Version: {new_version}\n",
            1,
        )
        new_meta_bytes = new_meta_text.encode("utf-8")

        new_record_lines: list[str] = []
        for line in old_record_text.splitlines():
            if not line.strip():
                continue
            rel, _, _ = line.partition(",")
            new_rel = rel.replace(old_dist_info, new_dist_info, 1)
            if new_rel == f"{new_dist_info}/RECORD":
                new_record_lines.append(f"{new_rel},,")
            elif new_rel == f"{new_dist_info}/METADATA":
                digest = base64.urlsafe_b64encode(hashlib.sha256(new_meta_bytes).digest()).rstrip(b"=").decode("ascii")
                new_record_lines.append(f"{new_rel},sha256={digest},{len(new_meta_bytes)}")
            else:
                # Body unchanged for everything else; just rewrite the path.
                _, _, hash_size = line.partition(",")
                new_record_lines.append(f"{new_rel},{hash_size}")
        new_record_text = "\n".join(new_record_lines) + "\n"

        out_path = wheel_path.with_name(wheel_path.name.replace(f"-{old_version}-", f"-{new_version}-", 1))
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
            for info in zf_in.infolist():
                if info.filename.startswith(f"{old_dist_info}/"):
                    suffix = info.filename[len(old_dist_info) + 1 :]
                    new_name = f"{new_dist_info}/{suffix}"
                    if suffix == "METADATA":
                        zf_out.writestr(new_name, new_meta_bytes)
                    elif suffix == "RECORD":
                        zf_out.writestr(new_name, new_record_text.encode("utf-8"))
                    else:
                        zf_out.writestr(new_name, zf_in.read(info.filename))
                else:
                    zf_out.writestr(info, zf_in.read(info.filename))

    wheel_path.unlink()
    return out_path


def _find_dist_info_dir(names: list[str]) -> str:
    """Return the single ``*.dist-info`` directory entry from *names*.

    ``names`` is the output of :meth:`zipfile.ZipFile.namelist`.  We
    look for any path component matching ``*.dist-info`` and return the
    first such prefix without trailing slash (e.g.
    ``"agent_foundry-0.1.0.dist-info"``).
    """
    seen: set[str] = set()
    for name in names:
        head = name.split("/", 1)[0]
        if head.endswith(".dist-info"):
            seen.add(head)
    if len(seen) != 1:
        raise RuntimeError(f"expected exactly one *.dist-info dir in wheel, got: {sorted(seen)}")
    return next(iter(seen))


def _extract_version(metadata_text: str) -> str:
    """Return the value of the ``Version:`` header in METADATA."""
    for line in metadata_text.splitlines():
        if line.startswith("Version: "):
            return line[len("Version: ") :].strip()
    raise RuntimeError("no `Version:` header in METADATA")


def _ordered_extras(extras: tuple[str, ...]) -> list[str]:
    """Return payload-requested extras in order, de-duplicated."""
    return list(dict.fromkeys(extras))


def _copy_constraint_files(staging: Path, extras: list[str]) -> list[str]:
    """Write committed constraint files into *staging*; return their names."""
    from agent_foundry.constraints import (
        APP_CONSTRAINT_EXTRAS,
        load_constraint_text,
        staging_filename,
    )

    names: list[str] = []
    for extra in extras:
        if extra not in APP_CONSTRAINT_EXTRAS:
            raise RuntimeError(
                f"no lock-derived App constraints for extra {extra!r}; "
                "add it to APP_CONSTRAINT_EXTRAS and run "
                "`python -m agent_foundry.constraints.export`"
            )
        dest = staging / staging_filename(extra)
        dest.write_text(load_constraint_text(extra), encoding="utf-8")
        names.append(dest.name)
    return names


def _write_requirements(
    staging: Path,
    wheel_filename: str,
    *,
    extras: tuple[str, ...] = ("runtime",),
) -> None:
    """Write ``requirements.txt`` referencing the local wheel.

    *extras* is the payload's dependency contract.  Agent payloads request
    ``runtime`` (plus ``extraction`` when enabled); the lean REST-to-MCP
    proxy requests only ``rest_mcp``.

    Lock-derived constraint files are always copied into *staging* and
    referenced with pip ``-c`` lines so the App container cannot float
    inside the extra ranges.
    """
    ordered = _ordered_extras(extras)
    extras_spec = ",".join(ordered)
    lines = [
        "# Generated by agent_foundry.deploy.staging.build_deploy_staging",
        f"# Installs the framework wheel + its [{extras_spec}] extras into",
        "# the Databricks App container at deploy time.  See issue #112.",
        f"./{wheel_filename}[{extras_spec}]",
    ]
    for name in _copy_constraint_files(staging, ordered):
        lines.append(f"-c {name}")
    (staging / "requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_instance(instance_dir: Path, staging: Path) -> None:
    """Copy the instance dir into ``<staging>/instances/<name>/``.

    Excludes deploy-time-only caches (see ``_INSTANCE_COPY_EXCLUDES``).
    """
    target = staging / "instances" / instance_dir.name
    shutil.copytree(
        instance_dir,
        target,
        ignore=shutil.ignore_patterns(*_INSTANCE_COPY_EXCLUDES),
    )


def clean_workspace_dir(
    ws: WorkspaceClient,
    workspace_path: str,
    *,
    dry_run: bool,
) -> bool:
    """Delete ``workspace_path`` recursively before sync; True if it existed and was deleted.

    Issue #112 workaround: ``databricks sync --full`` does NOT delete
    remote files that are absent locally
    (https://github.com/databricks/databricks-cli/issues/688), so the
    first wheel-based deploy on top of a previous Option-A deploy
    would leave stale ``agent_foundry/``, ``bricks/``, ``docs/``, etc.
    in the workspace dir.  Pre-cleaning closes that gap.

    Tolerates "path does not exist" so the first-ever deploy works.
    The Databricks SDK raises a typed ``NotFound`` whose ``str(exc)``
    is the bare message ``Path (...) doesn't exist.`` -- substring
    matching on ``"NOT_FOUND"`` / ``"RESOURCE_DOES_NOT_EXIST"`` is
    therefore unreliable across SDK versions.  Catch the typed
    exception first and fall back to the substring check only for
    legacy/atypical wrappers.  Other errors propagate -- a
    partially-cleaned workspace is recoverable (the sync that follows
    would rebuild it) but the operator should know.
    """
    # Imported locally so static analysis tools that run without the
    # SDK installed (the wheel-build container) do not fail; the
    # function is only ever called from a real deploy where the SDK
    # is present.
    from databricks.sdk.errors import NotFound

    if dry_run:
        print(f"  [dry-run] Would delete {workspace_path} (recursive)")
        return False

    print(f"  Cleaning workspace dir {workspace_path} (recursive) ...")
    try:
        ws.workspace.delete(workspace_path, recursive=True)
    except NotFound:
        print("  (workspace dir does not exist -- nothing to clean)")
        return False
    except Exception as exc:
        # Belt-and-braces: keep the legacy substring check so we still
        # tolerate older SDK wrappers (or test mocks that raise a bare
        # ``Exception("RESOURCE_DOES_NOT_EXIST")`` rather than the
        # typed class).
        msg = str(exc)
        if "NOT_FOUND" in msg or "RESOURCE_DOES_NOT_EXIST" in msg:
            print("  (workspace dir does not exist -- nothing to clean)")
            return False
        raise
    return True
