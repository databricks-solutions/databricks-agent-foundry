"""Guard every shipped SQL template against unquoted catalog/schema identifiers.

Unity Catalog accepts hyphens in a catalog name (for example
``acme_platform-eu-dev``) but Spark SQL rejects a
hyphenated name in an unquoted identifier position:

    The unquoted identifier acme_platform-eu-dev is invalid and must be
    back quoted as: `acme_platform-eu-dev`

So every ``${var.catalog}``/``${var.schema}`` that lands in an identifier
position has to be back-quoted, which is what ``sql_statements.ensure_schema``
already does when it builds SQL in Python.

Two positions need care and are pinned below.  Volume paths
(``/Volumes/${var.catalog}/${var.schema}/...``) are string literals rather than
identifiers, so a back tick there would become part of the path.  The
``source:`` key of a metric-view YAML body does need the back ticks, but only
inside a quoted YAML scalar: the body is parsed as YAML first, and a leading
back tick cannot start a plain scalar.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SQL_ROOTS = ("bricks", "examples")

# ``${var.catalog}`` or ``${var.schema}`` used as the leading part of a dotted
# identifier without a preceding back tick.
UNQUOTED_IDENTIFIER_RE = re.compile(r"(?<!`)\$\{var\.(?:catalog|schema)\}\s*\.")
QUOTED_PREFIX = "`${var.catalog}`.`${var.schema}`."

# ``agent_schema`` is two-part (``catalog.schema``), so no pair of back ticks can
# quote it: `` `cat.sch_agent` `` names a single schema called "cat.sch_agent".
AGENT_SCHEMA_RE = re.compile(r"\$\{var\.agent_schema\}\s*\.")
QUOTED_AGENT_PREFIX = "`${var.catalog}`.`${var.agent_schema_name}`."


def _sql_files() -> list[Path]:
    return sorted(path for root in SQL_ROOTS for path in (REPO_ROOT / root).rglob("*.sql"))


def _is_exempt(line: str) -> bool:
    """Comments describe the target rather than addressing it."""
    return line.strip().startswith("--")


def test_sql_files_are_discovered() -> None:
    """Keeps the scan below from passing vacuously if the globs ever break."""
    files = _sql_files()
    assert len(files) > 20
    assert any(QUOTED_PREFIX in path.read_text(encoding="utf-8") for path in files)


def test_sql_templates_back_quote_the_catalog_and_schema() -> None:
    offenders: list[str] = []
    for path in _sql_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _is_exempt(line):
                continue
            if UNQUOTED_IDENTIFIER_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert not offenders, "Back-quote the catalog and schema:\n" + "\n".join(offenders)


def test_sql_templates_address_the_agent_schema_in_two_parts() -> None:
    """SQL must compose catalog + schema name; only API fields take ``agent_schema``."""
    offenders: list[str] = []
    for path in _sql_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _is_exempt(line):
                continue
            if AGENT_SCHEMA_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert not offenders, f"Address the agent schema as {QUOTED_AGENT_PREFIX}:\n" + "\n".join(offenders)


def test_the_recommended_agent_schema_form_is_actually_used() -> None:
    """Keeps the guard above from passing because no template touches the agent schema."""
    assert any(QUOTED_AGENT_PREFIX in path.read_text(encoding="utf-8") for path in _sql_files())


def test_volume_paths_stay_unquoted() -> None:
    """``/Volumes/...`` is a string literal; a back tick there is part of the path."""
    for path in _sql_files():
        text = path.read_text(encoding="utf-8")
        assert "/Volumes/`" not in text, f"{path.relative_to(REPO_ROOT)} quotes a volume path"


def test_metric_view_yaml_source_is_back_quoted_inside_a_yaml_scalar() -> None:
    """Bare back ticks fail YAML parsing; a bare name fails to resolve a hyphen."""
    metric_views = [path for path in _sql_files() if "metric_views" in path.parts]
    assert metric_views, "expected at least one metric-view template"

    sources = [
        line.strip()
        for path in metric_views
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("source:")
    ]
    assert sources, "expected at least one YAML source key"

    for source in sources:
        value = source.split(":", 1)[1].strip()
        assert value.startswith(('"', "'")), f"YAML source must be a quoted scalar: {source}"
        assert QUOTED_PREFIX in value, f"YAML source must back-quote the name: {source}"
