"""Report-only cleanup scan for leaked ephemeral acceptance Apps."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

_EPHEMERAL = re.compile(r"(^|-)af-(rm|pa)-[a-z0-9]{1,6}(-app)?$")


def matching_app_names(names: list[str]) -> list[str]:
    """Return App names that look like upstream harness leftovers."""
    return sorted(name for name in names if _EPHEMERAL.search(name))


def report_apps(ws: Any) -> list[str]:
    listed = ws.apps.list()
    names: list[str] = []
    for app in listed:
        name = getattr(app, "name", None)
        if name:
            names.append(str(name))
    return matching_app_names(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_foundry.testing.janitor")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Reserved; deletion is disabled until prefix/age filtering is reviewed.",
    )
    args = parser.parse_args(argv)
    if args.delete:
        print("janitor deletion is disabled (report-only)", file=sys.stderr)
        return 2
    from agent_foundry.testing.workspace import load_workspace_binding
    from agent_foundry.workspace_client import get_workspace_client

    binding = load_workspace_binding()
    ws = get_workspace_client({"host": binding.host, "profile": binding.profile or ""})
    leaked = report_apps(ws)
    json.dump({"matching_apps": leaked, "mode": "report-only"}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
