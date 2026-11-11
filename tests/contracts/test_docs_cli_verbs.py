"""CLI verb drift guard (doc-architecture D5, Chunk 4).

``docs/reference/cli.md`` is hand-authored prose -- its examples and
rationale carry far more value than a ``--help`` dump would. The one part
that *is* mechanically derivable is the top-level verb list, and that list
silently drifts whenever a verb is added or renamed in the Typer app
(``agent-foundry config``, ``sync-schemas``, ``scaffold``, and ``init`` had
all already gone missing from the table before this guard existed).

This test pins the verb table to the live Typer app without owning the rest
of the file: it asserts the set of verbs documented under the ``## Verbs``
heading equals the set of commands and command groups registered on
``agent_foundry.deploy.cli.app``.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_foundry.deploy.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_DOC = REPO_ROOT / "docs" / "reference" / "cli.md"

# First column of a verb-table row, e.g. ``| `agent-foundry deploy` | ... |``
# or ``| `agent-foundry monitors {start,stop}` | ... |``. We only care about
# the verb token immediately after ``agent-foundry``.
_VERB_CELL = re.compile(r"^\|\s*`agent-foundry ([a-z][\w-]*)")


def _typer_verbs() -> set[str]:
    """Top-level commands and command groups registered on the CLI app."""
    verbs: set[str] = set()
    for cmd in app.registered_commands:
        if cmd.name:
            verbs.add(cmd.name)
        elif cmd.callback is not None:
            verbs.add(cmd.callback.__name__.replace("_", "-"))
    verbs.update(grp.name for grp in app.registered_groups if grp.name)
    return verbs


def _documented_verbs() -> set[str]:
    """Verb tokens listed in the ``## Verbs`` table of ``cli.md``."""
    text = CLI_DOC.read_text(encoding="utf-8")
    section = text.split("## Verbs", 1)[1].split("\n## ", 1)[0]
    return {match.group(1) for line in section.splitlines() if (match := _VERB_CELL.match(line.strip()))}


def test_cli_doc_verb_table_matches_typer_app() -> None:
    documented = _documented_verbs()
    actual = _typer_verbs()

    missing = actual - documented
    phantom = documented - actual
    assert not missing and not phantom, (
        "docs/reference/cli.md '## Verbs' table is out of sync with the "
        "agent-foundry CLI.\n"
        f"  Missing from the doc: {sorted(missing) or 'none'}\n"
        f"  Listed but not a verb: {sorted(phantom) or 'none'}\n"
        "Update the verb table in docs/reference/cli.md."
    )
