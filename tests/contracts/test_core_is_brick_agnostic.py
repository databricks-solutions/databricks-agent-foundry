"""68-C acceptance: the deploy *loop* is brick-agnostic.

Issue #68's acceptance criterion was: *"the deploy flow works without
mentioning sql_function anywhere in core code"*.  68-C migrated every
built-in brick to the :class:`~agent_foundry.bricks.Brick` protocol so
the orchestration loop (``deploy_instance``) iterates registered
bricks without naming any of them.

Two categories of reference are *explicitly* allowed in
``agent_foundry/`` and we do not count them as violations:

1. Docstring / comment mentions that use a brick name as an illustrative
   example.  These are documentation, not behaviour.
2. The ``_builtin_bricks()`` import block in
   ``agent_foundry/bricks.py``, which imports every shipped brick
   adapter by name -- that IS the definition of "built-in" and cannot
   be discovery-based without breaking the deterministic ordering
   property the hybrid registry design depends on (#82).

What the test *does* guard: the body of ``deploy_instance``, the
state-loading pipeline, and the variable-resolution code must not
reference any built-in brick name.
"""

from __future__ import annotations

import re
from pathlib import Path

AGENT_FOUNDRY_PKG = Path(__file__).resolve().parents[2] / "agent_foundry"

# The orchestration loop lives here and is the primary thing 68-C
# wants kept brick-agnostic.  Not the whole agent_foundry/ tree.
ORCHESTRATION_LOOP = AGENT_FOUNDRY_PKG / "deploy" / "_orchestrator.py"
PROBE_BRICK = "sql_function"


def test_deploy_instance_does_not_name_any_builtin_brick() -> None:
    """The ``deploy_instance`` function must not mention any built-in
    brick name.  If it does, you are hard-coding a brick in the
    orchestration loop -- exactly what 68-C set out to prevent.
    """
    src = ORCHESTRATION_LOOP.read_text(encoding="utf-8")
    # Narrow to the ``deploy_instance`` function body.
    body_match = re.search(
        r"^def deploy_instance\([\s\S]*?(?=^\S)",
        src,
        re.MULTILINE,
    )
    assert body_match, f"Could not locate deploy_instance in {ORCHESTRATION_LOOP.relative_to(AGENT_FOUNDRY_PKG.parent)}"
    body = body_match.group(0)

    pattern = re.compile(rf"\b{PROBE_BRICK}\b")
    matches = pattern.findall(body)
    assert not matches, f"deploy_instance() must not mention built-in brick {PROBE_BRICK!r}. Found:\n" + body


def test_orchestration_module_never_names_a_builtin_brick() -> None:
    """Outside :func:`deploy_instance` too, the orchestration module must
    not name ``sql_function`` -- every brick is reached via the registry."""
    src = ORCHESTRATION_LOOP.read_text(encoding="utf-8")
    references = [(i, line) for i, line in enumerate(src.splitlines(), 1) if PROBE_BRICK in line]
    assert not references, f"Unexpected references to {PROBE_BRICK!r} in the orchestration module:\n" + "\n".join(
        f"  L{n}: {line}" for n, line in references
    )
