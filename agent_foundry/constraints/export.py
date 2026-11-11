"""Regenerate App constraint files from ``uv.lock``.

Run from the framework repo root after ``uv lock``::

    python -m agent_foundry.constraints.export

Strips index URLs, the internal PyPI proxy, and hash pins so Databricks
Apps ``pip`` can install against the workspace index.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from agent_foundry.constraints import (
    APP_CONSTRAINT_EXTRAS,
    forbidden_snippets_in,
    package_filename,
)

_EXPORT_FLAGS: tuple[str, ...] = (
    "export",
    "--frozen",
    "--no-dev",
    "--no-hashes",
    "--no-emit-project",
)

_DROP_PREFIXES: tuple[str, ...] = (
    "--index-url",
    "--extra-index-url",
    "--find-links",
)


def repo_root_from(start: Path | None = None) -> Path:
    """Walk up from *start* (or this file) until ``pyproject.toml`` + ``uv.lock``."""
    here = (start or Path(__file__).resolve()).parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "uv.lock").is_file():
            return candidate
    raise FileNotFoundError("could not locate framework repo root (pyproject.toml + uv.lock)")


def constraints_dir(root: Path | None = None) -> Path:
    return (root or repo_root_from()) / "agent_foundry" / "constraints"


def sanitize_export(raw: str) -> str:
    """Drop pip index / hash lines; keep package pins and comments."""
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _DROP_PREFIXES):
            continue
        if forbidden_snippets_in(line):
            continue
        kept.append(line.rstrip())
    text = "\n".join(kept).strip() + "\n"
    return text


def export_extra(extra: str, *, root: Path | None = None) -> str:
    """Run ``uv export --extra <extra>`` and sanitize the result."""
    project = root or repo_root_from()
    cmd = ["uv", *_EXPORT_FLAGS, "--extra", extra]
    proc = subprocess.run(
        cmd,
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed (rc={proc.returncode}):\n{proc.stderr or proc.stdout}")
    sanitized = sanitize_export(proc.stdout)
    leftover = forbidden_snippets_in(sanitized)
    if leftover:
        raise RuntimeError(f"sanitized export for extra {extra!r} still contains {leftover}")
    if "openai-agents==" not in sanitized and extra == "runtime":
        raise RuntimeError("runtime export missing openai-agents pin; uv export output unexpected")
    return sanitized


def write_all(*, root: Path | None = None) -> list[Path]:
    """Write one constraint file per :data:`APP_CONSTRAINT_EXTRAS`."""
    project = root or repo_root_from()
    out_dir = constraints_dir(project)
    written: list[Path] = []
    for extra in APP_CONSTRAINT_EXTRAS:
        path = out_dir / package_filename(extra)
        path.write_text(export_extra(extra, root=project), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Framework repo root (default: discover from this file).",
    )
    args = parser.parse_args(argv)
    written = write_all(root=args.root)
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
