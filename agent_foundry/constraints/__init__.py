"""Lock-derived pip constraints for Databricks App installs.

``uv.lock`` is the source of truth.  ``python -m agent_foundry.constraints.export``
writes one file per App-installed extra.  Deploy staging copies those files
next to the generated ``requirements.txt`` as ``-c constraints-<extra>.txt``.

Do not put the internal PyPI proxy, ``--index-url``, or ``--hash`` lines in
these files -- Apps ``pip install -r`` must resolve against the workspace
index (usually public PyPI).
"""

from __future__ import annotations

from importlib.resources import files

# Wheel extras Databricks Apps can install. Each payload selects the extras
# it needs; staging copies only their matching constraint files.
APP_CONSTRAINT_EXTRAS: tuple[str, ...] = ("runtime", "extraction", "rest_mcp")

_FORBIDDEN_SNIPPETS: tuple[str, ...] = (
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "pypi-proxy",
    "--hash=",
    "--hash ",
)


def package_filename(extra: str) -> str:
    """Filename inside this package (``runtime.txt``)."""
    return f"{extra}.txt"


def staging_filename(extra: str) -> str:
    """Filename written into the deploy staging dir."""
    return f"constraints-{extra}.txt"


def load_constraint_text(extra: str) -> str:
    """Return the committed constraint file for *extra*."""
    if extra not in APP_CONSTRAINT_EXTRAS:
        raise KeyError(f"no App constraint extra named {extra!r}")
    return files(__name__).joinpath(package_filename(extra)).read_text(encoding="utf-8")


def forbidden_snippets_in(text: str) -> list[str]:
    """Return any snippets that must never appear in App constraint files."""
    lower = text.lower()
    return [s for s in _FORBIDDEN_SNIPPETS if s.lower() in lower]


__all__ = [
    "APP_CONSTRAINT_EXTRAS",
    "forbidden_snippets_in",
    "load_constraint_text",
    "package_filename",
    "staging_filename",
]
