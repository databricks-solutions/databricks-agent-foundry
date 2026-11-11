"""Locate the active instance directory and the runtime project root."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from agent_foundry import env

logger = logging.getLogger(__name__)


def resolve_instance_dir() -> Path | None:
    """Locate the active application instance directory.

    Checked in order:
      1. ``INSTANCE_DIR`` env var (absolute or project-relative)
      2. Walk up from this file looking for a ``foundry.toml`` instance marker
    """
    from agent_foundry._instance_marker import is_instance_dir

    env_val = env.read_optional(env.INSTANCE_DIR)
    if env_val:
        p = Path(env_val)
        if not p.is_absolute():
            p = project_root() / p
        return p if is_instance_dir(p) else None

    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if is_instance_dir(candidate):
            return candidate
    return None


def ensure_instance_on_sys_path() -> None:
    """Prepend the active instance dir to ``sys.path`` so instance-shipped
    Python is importable at runtime.

    A ``type: python`` tool's ``callable_path`` (and the ``reset`` /
    ``finalize`` hooks) reference a module by dotted path -- e.g.
    ``mda_tools.spec_tools.add_signal`` living at
    ``instances/<name>/mda_tools/spec_tools.py``.  The wheel-based deploy
    (#112) dropped ``PYTHONPATH=.`` and the instance dir is only *located*
    (via :func:`resolve_instance_dir`) to read skills / mcp_servers off
    disk -- it is never added to ``sys.path``.  Rooting imports at the
    instance dir here is what makes those dotted paths resolve in both the
    deployed App and ``agent-foundry run`` / ``repl`` local dev.

    Appends rather than prepends: instances ship *new* packages (e.g.
    ``mda_tools``, a vendored ``mda_report_spec``), they do not override
    stdlib or framework dependencies.  Appending keeps instance code
    importable while ensuring an instance module that happens to share a
    name with the stdlib or an installed package can never shadow it
    process-wide.  Idempotent (guarded on ``p not in sys.path``) and
    best-effort: a missing instance marker is a no-op, never an error.
    """
    instance_dir = resolve_instance_dir()
    if instance_dir is None:
        return
    p = str(instance_dir)
    if p not in sys.path:
        sys.path.append(p)
        logger.info("[imports] added instance dir to sys.path: %s", p)


# Runtime project-root resolution is explicit and env-var-driven (issue
# #143). The launchers (``app.yaml``, ``agent-foundry run``/``repl``,
# ``start_app.py``) all set ``AGENT_FOUNDRY_PROJECT_ROOT`` before
# invoking this module; we never walk ``Path(__file__).parents`` (which
# is unsafe after the wheel-install rollout in #112 because ``__file__``
# lives in ``site-packages``) and never silently fall back to
# ``Path.cwd()``.
def project_root() -> Path:
    try:
        val = env.read(env.AGENT_FOUNDRY_PROJECT_ROOT)
    except env.MissingEnvVar as exc:
        raise RuntimeError(
            "AGENT_FOUNDRY_PROJECT_ROOT is not set. The agent runtime "
            "requires an explicit project root. Set it via app.yaml, "
            "`agent-foundry run`/`repl`, or export "
            "AGENT_FOUNDRY_PROJECT_ROOT=$(pwd) before invoking the agent."
        ) from exc
    root = Path(val).resolve()
    if not root.is_dir():
        raise RuntimeError(f"AGENT_FOUNDRY_PROJECT_ROOT={val!r} is not a directory.")
    return root
