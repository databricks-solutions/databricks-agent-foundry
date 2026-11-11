"""Skill bundles: the files of one skill, locally (git) and in Unity Catalog.

A skill folder ``ai_gateway/skills/<skill_id>/`` holds ``SKILL.md`` plus
optional supporting files.  In the workspace the same files live under
the Files API path ``/Skills/<catalog>/<schema>/<skill_id>/``.  Both
sides are compared through :func:`content_hash`.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

from databricks.sdk.errors import NotFound

from agent_foundry.contracts.skills import (
    MAX_SKILL_ID_CHARS,
    SKILL_FILE,
    SkillDocumentError,
    is_skill_slug,
    parse_skill_md,
)
from bricks.ai_gateway.config import SCHEMA_FQN_PATTERN

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

MAX_SKILL_MD_CHARS: Final = 20_000
_SCHEMA_FQN = re.compile(SCHEMA_FQN_PATTERN)
_SKILL_NAME_PREFIX: Final = "skills/"

BundleFiles = Mapping[str, bytes]


class SkillBundleError(ValueError):
    """A skill folder or remote bundle breaks the skill contract."""


@dataclass(frozen=True)
class LocalSkill:
    """A validated skill folder: its id and files keyed by POSIX relative path."""

    skill_id: str
    files: BundleFiles

    @property
    def content_hash(self) -> str:
        return content_hash(self.files)


def content_hash(files: BundleFiles) -> str:
    """SHA-256 over every (path, bytes) pair, independent of listing order."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        digest.update(f"{rel}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def validate_skill_id(skill_id: str) -> str:
    if not is_skill_slug(skill_id):
        raise SkillBundleError(
            f"skill id {skill_id!r} must be 1-{MAX_SKILL_ID_CHARS} lowercase letters, digits and single hyphens"
        )
    return skill_id


def skill_fqn(schema_fqn: str, skill_id: str) -> str:
    if not _SCHEMA_FQN.fullmatch(schema_fqn):
        raise SkillBundleError(f"not a 'catalog.schema' name: {schema_fqn!r}")
    return f"{schema_fqn}.{validate_skill_id(skill_id)}"


def skill_resource_name(fqn: str) -> str:
    """The ``name`` the skills API takes: ``skills/<catalog>.<schema>.<skill_id>``."""
    return f"{_SKILL_NAME_PREFIX}{fqn}"


def bundle_root(fqn: str) -> str:
    """Files API directory of the skill *fqn*."""
    schema_fqn, _, skill_id = fqn.rpartition(".")
    catalog, schema = skill_fqn(schema_fqn, skill_id).split(".")[:2]
    return f"/Skills/{catalog}/{schema}/{skill_id}"


def safe_relative_path(rel: str) -> PurePosixPath:
    """*rel* as a relative path that cannot leave its bundle directory."""
    path = PurePosixPath(rel)
    if not rel or "\\" in rel or path.is_absolute() or any(part in ("", ".", "..") for part in rel.split("/")):
        raise SkillBundleError(f"unsafe bundle path {rel!r}")
    return path


def check_skill_md(skill_id: str, text: str) -> None:
    """``SKILL.md`` has agentskills.io frontmatter naming *skill_id*, within the size cap."""
    if len(text) > MAX_SKILL_MD_CHARS:
        raise SkillBundleError(f"{skill_id}: {SKILL_FILE} has {len(text)} chars, over the {MAX_SKILL_MD_CHARS} cap")
    try:
        document = parse_skill_md(text)
    except SkillDocumentError as exc:
        raise SkillBundleError(f"{skill_id}: {exc}") from exc
    if document.name != skill_id:
        raise SkillBundleError(f"{skill_id}: frontmatter name {document.name!r} must equal the folder name")


def _bundle_paths(skill_dir: Path, skill_id: str) -> list[Path]:
    """Files to deploy; dotfiles are skipped and symlinks refused so nothing outside the folder is uploaded."""
    paths: list[Path] = []
    for path in sorted(skill_dir.rglob("*")):
        rel_parts = path.relative_to(skill_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.is_symlink():
            raise SkillBundleError(f"{skill_id}: symlinks are not allowed ({path.relative_to(skill_dir)})")
        if path.is_file():
            paths.append(path)
    return paths


def read_local_skill(skill_dir: Path) -> LocalSkill:
    """Read and validate one skill folder; dotfiles and dot-directories are skipped."""
    skill_id = validate_skill_id(skill_dir.name)
    files = {path.relative_to(skill_dir).as_posix(): path.read_bytes() for path in _bundle_paths(skill_dir, skill_id)}
    if SKILL_FILE not in files:
        raise SkillBundleError(f"{skill_id}: missing {SKILL_FILE}")
    try:
        text = files[SKILL_FILE].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillBundleError(f"{skill_id}: {SKILL_FILE} is not UTF-8") from exc
    check_skill_md(skill_id, text)
    return LocalSkill(skill_id=skill_id, files=files)


def _relative_to_root(root: str, path: str) -> str:
    prefix = f"{root}/"
    if not path.startswith(prefix):
        raise SkillBundleError(f"listed path {path!r} is outside {root!r}")
    return safe_relative_path(path[len(prefix) :].rstrip("/")).as_posix()


def list_remote_paths(ws: WorkspaceClient, fqn: str) -> list[str]:
    """Relative paths of every file in the remote bundle; none when the directory is absent."""
    root = bundle_root(fqn)
    paths: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(ws.files.list_directory_contents(directory))
        except NotFound:
            continue
        for entry in entries:
            full = (entry.path or "").rstrip("/")
            if entry.is_directory:
                _relative_to_root(root, full)
                pending.append(full)
            else:
                paths.append(_relative_to_root(root, full))
    return sorted(paths)


def download_bundle(ws: WorkspaceClient, fqn: str) -> dict[str, bytes]:
    """Every remote file of *fqn*, keyed by relative path."""
    root = bundle_root(fqn)
    files: dict[str, bytes] = {}
    for rel in list_remote_paths(ws, fqn):
        contents = ws.files.download(f"{root}/{rel}").contents
        files[rel] = contents.read() if contents is not None else b""
    return files


def upload_bundle(ws: WorkspaceClient, fqn: str, files: BundleFiles) -> None:
    root = bundle_root(fqn)
    for rel, data in sorted(files.items()):
        ws.files.upload(f"{root}/{safe_relative_path(rel)}", io.BytesIO(data), overwrite=True)


def delete_remote_files(ws: WorkspaceClient, fqn: str, rels: list[str]) -> None:
    root = bundle_root(fqn)
    for rel in rels:
        ws.files.delete(f"{root}/{safe_relative_path(rel)}")


__all__ = [
    "MAX_SKILL_MD_CHARS",
    "SKILL_FILE",
    "BundleFiles",
    "LocalSkill",
    "SkillBundleError",
    "bundle_root",
    "check_skill_md",
    "content_hash",
    "delete_remote_files",
    "download_bundle",
    "list_remote_paths",
    "read_local_skill",
    "safe_relative_path",
    "skill_fqn",
    "skill_resource_name",
    "upload_bundle",
    "validate_skill_id",
]
