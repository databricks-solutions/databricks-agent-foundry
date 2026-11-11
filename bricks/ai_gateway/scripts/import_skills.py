"""Import skills edited in the workspace UI back into git.

Downloads each skill of the configured ``skills.schema`` into
``<instance>/ai_gateway/skills/<skill_id>/``, replacing the local folder
so it mirrors the remote bundle.  Writes no state: the next deploy finds
the remote content equal to git, uploads nothing and only refreshes the
recorded etag.

Usage:
    python bricks/ai_gateway/scripts/import_skills.py --instance <name> [--target dev] [--skill <id> ...]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from databricks.sdk.errors.base import DatabricksError

from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.variables import add_project_root_arg
from bricks.ai_gateway.config import load_config, skills_root
from bricks.ai_gateway.skill_bundle import (
    SKILL_FILE,
    BundleFiles,
    SkillBundleError,
    check_skill_md,
    download_bundle,
    safe_relative_path,
    skill_fqn,
    validate_skill_id,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient


def remote_skill_ids(ws: WorkspaceClient, schema_fqn: str) -> list[str]:
    """Ids of every skill in *schema_fqn* (``Skill.name`` is ``skills/<catalog>.<schema>.<id>``)."""
    skills = ws.ai_gateway.list_skills(parent=f"schemas/{schema_fqn}")
    return sorted((skill.name or "").rsplit(".", 1)[-1] for skill in skills)


def check_bundle(skill_id: str, files: BundleFiles) -> None:
    """The downloaded bundle is a valid brick skill folder."""
    if SKILL_FILE not in files:
        raise SkillBundleError(f"{skill_id}: remote bundle has no {SKILL_FILE}")
    try:
        text = files[SKILL_FILE].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillBundleError(f"{skill_id}: remote {SKILL_FILE} is not UTF-8") from exc
    check_skill_md(skill_id, text)


def write_skill_folder(root: Path, skill_id: str, files: BundleFiles) -> Path:
    """Replace ``root/<skill_id>`` with exactly *files*; no path may leave that folder."""
    dest = root / validate_skill_id(skill_id)
    staging = root / f".{skill_id}.import"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    base = staging.resolve()
    for rel, data in files.items():
        path = (staging / safe_relative_path(rel)).resolve()
        if not path.is_relative_to(base):
            shutil.rmtree(staging, ignore_errors=True)
            raise SkillBundleError(f"{skill_id}: bundle path {rel!r} escapes the skill folder")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    return dest


def import_skills(
    instance_dir: Path,
    variables: dict[str, str],
    ws: WorkspaceClient,
    skill_ids: Sequence[str] = (),
) -> int:
    """Import *skill_ids* (default: every skill in the schema); 1 when any skill failed."""
    section = load_config(instance_dir, variables).skills
    if section is None:
        print("Error: ai_gateway/config.yml has no 'skills' section", file=sys.stderr)
        return 1
    ids = list(skill_ids) or remote_skill_ids(ws, section.schema_fqn)
    failures = 0
    for skill_id in ids:
        try:
            fqn = skill_fqn(section.schema_fqn, skill_id)
            files = download_bundle(ws, fqn)
            check_bundle(skill_id, files)
            dest = write_skill_folder(skills_root(instance_dir), skill_id, files)
        except (DatabricksError, SkillBundleError, OSError) as exc:
            failures += 1
            print(f"  [skills] {skill_id}: import FAILED: {exc}", file=sys.stderr)
            continue
        print(f"  [skills] Imported {fqn} -> {dest} ({len(files)} file(s))")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import workspace-edited skills into ai_gateway/skills/")
    parser.add_argument("--instance", required=True, help="Instance name")
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--skill", action="append", default=[], help="Skill id to import (repeatable; default all)")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra key=value vars")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    instance_dir = args.project_root / "instances" / args.instance
    if not instance_dir.is_dir():
        print(f"Error: Instance not found: {instance_dir}", file=sys.stderr)
        return 1
    variables = load_brick_variables(args.project_root, args.target, instance_dir, extra_vars=args.vars)
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    if ws is None:
        print("Error: no workspace credentials for this target", file=sys.stderr)
        return 1
    return import_skills(instance_dir, variables, ws, args.skill)


if __name__ == "__main__":
    raise SystemExit(main())
