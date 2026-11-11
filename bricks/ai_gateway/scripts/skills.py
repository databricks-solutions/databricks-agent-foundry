"""Deploy git-managed skills to Unity Catalog; git wins.

For each ``<instance>/ai_gateway/skills/<skill_id>/`` folder, as the
deploying principal (who becomes the owner):

1. Validate ``SKILL.md`` (frontmatter ``name`` / ``description``, size cap).
2. Create the skill when missing.
3. Skip it when the remote etag and the local content hash both match
   state -- nothing is uploaded and the etag stays stable, so agents keep
   their prompt cache.
4. Otherwise compare the remote files with git.  When they differ, upload
   every file, delete remote-only files and finalize.  Remote edits made
   since the last deploy are overwritten and reported, never lost silently.

Skills the brick created but git no longer has are deleted.  The brick
never touches skills it did not create.  ``destroy`` deletes the skills
in state, then any schema the brick created when it is empty.

Usage:
    python bricks/ai_gateway/scripts/skills.py --instance <name> [--target dev] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.catalog import Skill
from pydantic import BaseModel, ConfigDict, Field

from agent_foundry import state as shared_state
from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.destroy_base import uc_object_exists
from agent_foundry.variables import add_project_root_arg
from bricks.ai_gateway.config import load_config, skill_dirs
from bricks.ai_gateway.skill_bundle import (
    LocalSkill,
    SkillBundleError,
    content_hash,
    delete_remote_files,
    download_bundle,
    read_local_skill,
    skill_fqn,
    skill_resource_name,
    upload_bundle,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

STATE_BRICK_TYPE: Final = "ai_gateway_skills"
STATE_SUB_INSTANCE: Final = "skills"
SKILL_COMMENT: Final = "Managed by agent-foundry (ai_gateway brick); edits are overwritten on deploy."
SCHEMA_COMMENT: Final = "Created by agent-foundry (ai_gateway brick) for git-managed skills."

SyncOutcome = Literal["created", "updated", "overwrote_remote", "unchanged"]


class DeployedSkill(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    etag: str
    content_hash: str


class SkillsState(BaseModel):
    """Workspace state of the skills section; ``_audit`` and future fields are ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_fqn: str = Field(default="", alias="schema")
    created_schemas: list[str] = Field(default_factory=list)
    skills: dict[str, DeployedSkill] = Field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult:
    outcome: SyncOutcome
    record: DeployedSkill
    removed_files: tuple[str, ...] = ()


@dataclass
class SkillsReport:
    """What a deploy did, for the console and :class:`~agent_foundry.bricks.BrickResult` details."""

    changed: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    removed_files: dict[str, list[str]] = field(default_factory=dict)
    pruned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def rc(self) -> int:
        return 1 if self.errors else 0

    def details(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if value}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_skills_state(ws: Any | None, instance_dir: Path, target: str) -> SkillsState:
    raw = shared_state.load_state(ws, instance_dir.name, STATE_BRICK_TYPE, STATE_SUB_INSTANCE, target)
    return SkillsState.model_validate(raw)


def _save_skills_state(ws: Any, instance_dir: Path, target: str, state: SkillsState, *, deployer: str) -> None:
    shared_state.save_state(
        ws,
        instance_dir.name,
        STATE_BRICK_TYPE,
        STATE_SUB_INSTANCE,
        target,
        state.model_dump(mode="json", by_alias=True),
        action="deploy",
        deployer=deployer,
    )


# ---------------------------------------------------------------------------
# Remote skill operations
# ---------------------------------------------------------------------------


def _get_skill(ws: WorkspaceClient, fqn: str) -> Skill | None:
    try:
        return ws.ai_gateway.get_skill(name=skill_resource_name(fqn))
    except NotFound:
        return None


def _publish(
    ws: WorkspaceClient,
    fqn: str,
    local: LocalSkill,
    removed: tuple[str, ...],
    outcome: SyncOutcome,
) -> SyncResult:
    upload_bundle(ws, fqn, local.files)
    delete_remote_files(ws, fqn, list(removed))
    finalized = ws.ai_gateway.finalize_skill(name=skill_resource_name(fqn))
    return SyncResult(outcome, DeployedSkill(etag=finalized.etag or "", content_hash=local.content_hash), removed)


def _create(ws: WorkspaceClient, fqn: str, local: LocalSkill) -> SyncResult:
    schema_fqn = fqn.rsplit(".", 1)[0]
    ws.ai_gateway.create_skill(Skill(comment=SKILL_COMMENT), parent=f"schemas/{schema_fqn}", skill_id=local.skill_id)
    try:
        return _publish(ws, fqn, local, (), "created")
    except Exception:
        ws.ai_gateway.delete_skill(name=skill_resource_name(fqn))
        raise


def sync_skill(ws: WorkspaceClient, fqn: str, local: LocalSkill, previous: DeployedSkill | None) -> SyncResult:
    """Make the remote skill *fqn* equal *local*, uploading only when the content differs."""
    remote = _get_skill(ws, fqn)
    if remote is None:
        return _create(ws, fqn, local)
    if previous == DeployedSkill(etag=remote.etag or "", content_hash=local.content_hash):
        return SyncResult("unchanged", previous)
    remote_files = download_bundle(ws, fqn)
    if content_hash(remote_files) == local.content_hash:
        return SyncResult("unchanged", DeployedSkill(etag=remote.etag or "", content_hash=local.content_hash))
    edited_remotely = previous is None or previous.etag != remote.etag
    removed = tuple(sorted(set(remote_files) - set(local.files)))
    return _publish(ws, fqn, local, removed, "overwrote_remote" if edited_remotely else "updated")


def delete_skill(ws: WorkspaceClient, fqn: str) -> None:
    try:
        ws.ai_gateway.delete_skill(name=skill_resource_name(fqn))
    except NotFound:
        pass


def ensure_schema(ws: WorkspaceClient, schema_fqn: str) -> bool:
    """Create *schema_fqn* when missing; ``True`` when this call created it."""
    if uc_object_exists(ws.schemas.get, schema_fqn):
        return False
    catalog, schema = schema_fqn.split(".")
    ws.schemas.create(name=schema, catalog_name=catalog, comment=SCHEMA_COMMENT)
    print(f"  [skills] Created schema {schema_fqn}")
    return True


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


def read_local_skills(instance_dir: Path) -> tuple[list[LocalSkill], list[str]]:
    """Every valid skill folder, plus one error per invalid folder."""
    skills: list[LocalSkill] = []
    errors: list[str] = []
    for skill_dir in skill_dirs(instance_dir):
        try:
            skills.append(read_local_skill(skill_dir))
        except SkillBundleError as exc:
            errors.append(str(exc))
    return skills, errors


def _record(report: SkillsReport, fqn: str, result: SyncResult) -> None:
    if result.outcome == "unchanged":
        print(f"  [skills] {fqn}: unchanged")
        return
    report.changed.append(fqn)
    if result.outcome == "overwrote_remote":
        report.overwritten.append(fqn)
        print(f"  [skills] {fqn}: OVERWROTE remote edits with the git version")
    else:
        print(f"  [skills] {fqn}: {result.outcome}")
    if result.removed_files:
        report.removed_files[fqn] = list(result.removed_files)
        print(f"  [skills] {fqn}: removed remote-only files {list(result.removed_files)}")


def _prune(ws: WorkspaceClient, state: SkillsState, desired: set[str], report: SkillsReport) -> None:
    for fqn in sorted(set(state.skills) - desired):
        try:
            delete_skill(ws, fqn)
        except DatabricksError as exc:
            report.errors.append(f"{fqn}: delete failed: {exc}")
            continue
        del state.skills[fqn]
        report.pruned.append(fqn)
        print(f"  [skills] {fqn}: deleted (no longer in git)")


def deploy_skills(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    dry_run: bool = False,
    ws: WorkspaceClient | None = None,
) -> SkillsReport:
    """Sync every skill folder to the configured schema; never raises for one bad skill."""
    report = SkillsReport()
    section = load_config(instance_dir, variables).skills
    if section is None:
        return report
    skills, report.errors = read_local_skills(instance_dir)
    if report.errors:
        for error in report.errors:
            print(f"  [skills] INVALID: {error}", file=sys.stderr)
        return report
    fqns = {skill_fqn(section.schema_fqn, skill.skill_id): skill for skill in skills}
    if dry_run or ws is None:
        print(f"  [DRY RUN] Would sync {len(fqns)} skill(s) to {section.schema_fqn}: {sorted(fqns)}")
        return report

    deployer = variables.get("deployer", "")
    state = load_skills_state(ws, instance_dir, target)
    try:
        if ensure_schema(ws, section.schema_fqn):
            state.created_schemas.append(section.schema_fqn)
    except DatabricksError as exc:
        report.errors.append(f"{section.schema_fqn}: schema unavailable: {exc}")
        print(f"  [skills] Schema {section.schema_fqn} unavailable: {exc}", file=sys.stderr)
        return report
    state.schema_fqn = section.schema_fqn
    for fqn, local in sorted(fqns.items()):
        try:
            result = sync_skill(ws, fqn, local, state.skills.get(fqn))
        except (DatabricksError, SkillBundleError) as exc:
            report.errors.append(f"{fqn}: {exc}")
            print(f"  [skills] {fqn}: FAILED: {exc}", file=sys.stderr)
            continue
        _record(report, fqn, result)
        state.skills[fqn] = result.record
        _save_skills_state(ws, instance_dir, target, state, deployer=deployer)
    _prune(ws, state, set(fqns), report)
    _save_skills_state(ws, instance_dir, target, state, deployer=deployer)
    return report


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DestroyOutcome:
    rc: int
    deleted_skills: tuple[str, ...] = ()
    dropped_schemas: tuple[str, ...] = ()


def _drop_schema_if_empty(ws: WorkspaceClient, schema_fqn: str) -> bool:
    """Non-forced delete: Unity Catalog refuses while anything is left in the schema."""
    try:
        ws.schemas.delete(full_name=schema_fqn)
    except NotFound:
        return False
    except DatabricksError as exc:
        print(f"  [skills] Schema {schema_fqn} preserved (not empty or not droppable): {exc}")
        return False
    print(f"  [skills] Dropped schema {schema_fqn}")
    return True


def destroy_skills(instance_dir: Path, target: str, *, dry_run: bool, ws: WorkspaceClient | None) -> DestroyOutcome:
    """Delete the skills in state, then the schemas this brick created when empty."""
    state = load_skills_state(ws, instance_dir, target)
    if dry_run or ws is None:
        for fqn in sorted(state.skills):
            print(f"  [DRY RUN] Would delete skill {fqn}")
        return DestroyOutcome(rc=0)
    deleted: list[str] = []
    failures = 0
    for fqn in sorted(state.skills):
        try:
            delete_skill(ws, fqn)
        except DatabricksError as exc:
            failures += 1
            print(f"  [skills] {fqn}: delete failed: {exc}", file=sys.stderr)
            continue
        deleted.append(fqn)
        print(f"  [skills] Deleted skill {fqn}")
    if failures:
        return DestroyOutcome(rc=1, deleted_skills=tuple(deleted))
    dropped = tuple(schema for schema in state.created_schemas if _drop_schema_if_empty(ws, schema))
    shared_state.delete_state(ws, instance_dir.name, STATE_BRICK_TYPE, STATE_SUB_INSTANCE, target)
    return DestroyOutcome(rc=0, deleted_skills=tuple(deleted), dropped_schemas=dropped)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy git-managed skills to Unity Catalog (git wins)")
    parser.add_argument("--instance", required=True, help="Instance name")
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview only")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra key=value vars")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    instance_dir = args.project_root / "instances" / args.instance
    if not instance_dir.is_dir():
        print(f"Error: Instance not found: {instance_dir}", file=sys.stderr)
        return 1
    variables = load_brick_variables(args.project_root, args.target, instance_dir, extra_vars=args.vars)
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = None if args.dry_run else prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    return deploy_skills(instance_dir, args.target, variables, dry_run=args.dry_run, ws=ws).rc


if __name__ == "__main__":
    raise SystemExit(main())
