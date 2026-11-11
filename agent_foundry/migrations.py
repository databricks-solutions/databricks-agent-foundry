"""In-place rewrites for project-owned files the framework cannot reach.

A project scaffolds its own copies of the shipped SQL templates, so a fix to
``bricks/**/_scaffold`` never reaches an existing project.  ``quote_identifiers``
applies the identifier-quoting rules to those copies; the rules themselves are
pinned by ``bricks/data/tests/unit/test_sql_identifier_quoting.py``:

* ``${var.catalog}``/``${var.schema}`` in an identifier position must be
  back-quoted individually, because Unity Catalog allows a hyphen where Spark
  SQL requires quoting.
* ``${var.agent_schema}`` is two-part and cannot be quoted at all -- it becomes
  `` `${var.catalog}`.`${var.agent_schema_name}` ``.
* Volume paths are string literals, so they stay bare.  They need no exemption
  here: their parts are separated by ``/`` rather than ``.``.
* A metric view's ``source:`` needs the back ticks *inside* a quoted YAML
  scalar, since the body is parsed as YAML before the name is resolved and a
  leading back tick cannot start a plain scalar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ``${var.catalog}`` or ``${var.schema}`` heading a dotted identifier, with no
# back tick already in front of it.
_UNQUOTED_PART_RE = re.compile(r"(?<!`)\$\{var\.(catalog|schema)\}(\s*\.)")
_AGENT_SCHEMA_RE = re.compile(r"`?\$\{var\.agent_schema\}`?(\s*\.)")
_YAML_SOURCE_RE = re.compile(r"^(?P<indent>\s*)source:\s*(?P<value>\S+)\s*$")

_QUOTED_AGENT_SCHEMA = "`${var.catalog}`.`${var.agent_schema_name}`"


@dataclass(frozen=True)
class FileRewrite:
    """One file's proposed rewrite."""

    path: Path
    original: str
    updated: str

    @property
    def changed(self) -> bool:
        return self.original != self.updated

    @property
    def changed_line_numbers(self) -> list[int]:
        before = self.original.splitlines()
        after = self.updated.splitlines()
        return [n for n, (a, b) in enumerate(zip(before, after), 1) if a != b]


def quote_identifiers(text: str) -> str:
    """Return *text* with catalog/schema identifiers back-quoted.

    Idempotent: already-quoted templates are returned unchanged.
    """
    return "\n".join(_rewrite_line(line) for line in text.split("\n"))


def _rewrite_line(line: str) -> str:
    if line.lstrip().startswith("--"):
        # A comment describes the target rather than addressing it.
        return line

    rewritten = _AGENT_SCHEMA_RE.sub(rf"{_QUOTED_AGENT_SCHEMA}\1", line)
    rewritten = _UNQUOTED_PART_RE.sub(r"`${var.\1}`\2", rewritten)
    return _quote_yaml_source(rewritten)


def _quote_yaml_source(line: str) -> str:
    """Wrap a metric view's ``source:`` value in a YAML scalar when it needs one."""
    match = _YAML_SOURCE_RE.match(line)
    if match is None:
        return line

    value = match.group("value")
    if "`" not in value or value.startswith(('"', "'")):
        return line
    return f'{match.group("indent")}source: "{value}"'


def plan_quote_identifiers(paths: list[Path]) -> list[FileRewrite]:
    """Build the rewrite for each ``.sql`` file in *paths*, changed or not."""
    rewrites = []
    for path in paths:
        original = path.read_text(encoding="utf-8")
        rewrites.append(FileRewrite(path=path, original=original, updated=quote_identifiers(original)))
    return rewrites


def find_project_sql(project_root: Path, instance: str | None = None) -> list[Path]:
    """Every project-owned ``.sql`` file, i.e. authored under ``instances/``.

    Framework templates are excluded by construction -- they live in the
    installed package, not in the project's ``instances/`` tree.
    """
    root = project_root / "instances"
    if instance:
        root = root / instance
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.sql") if "_build" not in path.parts)


def apply_rewrites(rewrites: list[FileRewrite]) -> None:
    for rewrite in rewrites:
        if rewrite.changed:
            rewrite.path.write_text(rewrite.updated, encoding="utf-8")
