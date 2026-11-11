"""In-memory stand-ins for the skills, Files and schemas APIs, plus workspace state."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.catalog import Skill

from agent_foundry import state as shared_state

SCHEMA = "main.skills"


@dataclass
class FakeAiGateway:
    files: dict[str, bytes]
    skills: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    finalize_error: Exception | None = None
    _version: int = 0

    def _etag(self) -> str:
        self._version += 1
        return f"etag-{self._version}"

    def get_skill(self, name: str) -> Skill:
        if name not in self.skills:
            raise NotFound(name)
        return Skill(name=name, etag=self.skills[name])

    def create_skill(self, skill: Skill, parent: str, skill_id: str) -> Skill:
        self.calls.append(f"create {skill_id}")
        name = f"skills/{parent.removeprefix('schemas/')}.{skill_id}"
        self.skills[name] = ""
        return Skill(name=name, comment=skill.comment)

    def finalize_skill(self, name: str) -> Skill:
        self.calls.append(f"finalize {name}")
        if self.finalize_error is not None:
            raise self.finalize_error
        self.skills[name] = self._etag()
        return Skill(name=name, etag=self.skills[name])

    def delete_skill(self, name: str, *, etag: str | None = None) -> None:
        self.calls.append(f"delete {name}")
        if self.skills.pop(name, None) is None:
            raise NotFound(name)

    def list_skills(self, parent: str) -> list[Skill]:
        prefix = f"skills/{parent.removeprefix('schemas/')}."
        return [Skill(name=name) for name in self.skills if name.startswith(prefix)]

    def edit_remotely(self, name: str) -> None:
        """What a finalize from the workspace UI does to the etag."""
        self.skills[name] = self._etag()


@dataclass
class FakeFiles:
    files: dict[str, bytes]

    def upload(self, file_path: str, contents: io.BytesIO, *, overwrite: bool | None = None) -> None:
        self.files[file_path] = contents.read()

    def download(self, file_path: str) -> SimpleNamespace:
        return SimpleNamespace(contents=io.BytesIO(self.files[file_path]))

    def delete(self, file_path: str) -> None:
        del self.files[file_path]

    def list_directory_contents(self, directory_path: str) -> list[SimpleNamespace]:
        prefix = f"{directory_path}/"
        children = {path[len(prefix) :].split("/")[0] for path in self.files if path.startswith(prefix)}
        if not children:
            raise NotFound(directory_path)
        return [
            SimpleNamespace(path=f"{prefix}{child}", is_directory=f"{prefix}{child}" not in self.files)
            for child in sorted(children)
        ]


@dataclass
class FakeSchemas:
    existing: set[str] = field(default_factory=set)
    non_empty: set[str] = field(default_factory=set)

    def get(self, full_name: str) -> SimpleNamespace:
        if full_name not in self.existing:
            raise NotFound(full_name)
        return SimpleNamespace(full_name=full_name)

    def create(self, name: str, catalog_name: str, comment: str = "") -> None:
        self.existing.add(f"{catalog_name}.{name}")

    def delete(self, full_name: str) -> None:
        if full_name in self.non_empty:
            raise DatabricksError(f"Schema {full_name} is not empty")
        self.existing.discard(full_name)


class FakeWorkspace:
    def __init__(self) -> None:
        self.file_store: dict[str, bytes] = {}
        self.ai_gateway = FakeAiGateway(self.file_store)
        self.files = FakeFiles(self.file_store)
        self.schemas = FakeSchemas({SCHEMA})


@pytest.fixture
def ws() -> FakeWorkspace:
    return FakeWorkspace()


StateKey = tuple[str, str, str, str]


@dataclass
class FakeStateStore:
    """Workspace state, keyed by (instance, brick_type, sub_instance, target)."""

    entries: dict[StateKey, dict[str, Any]] = field(default_factory=dict)

    def load(self, _ws: Any, instance: str, brick_type: str, sub: str, target: str, **_kw: Any) -> dict[str, Any]:
        return dict(self.entries.get((instance, brick_type, sub, target), {}))

    def save(
        self, _ws: Any, instance: str, brick_type: str, sub: str, target: str, data: dict[str, Any], **_kw: Any
    ) -> None:
        self.entries[(instance, brick_type, sub, target)] = dict(data)

    def delete(self, _ws: Any, instance: str, brick_type: str, sub: str, target: str, **_kw: Any) -> None:
        self.entries.pop((instance, brick_type, sub, target), None)


@pytest.fixture
def state_store(monkeypatch: pytest.MonkeyPatch) -> dict[StateKey, dict[str, Any]]:
    store = FakeStateStore()
    monkeypatch.setattr(shared_state, "load_state", store.load)
    monkeypatch.setattr(shared_state, "save_state", store.save)
    monkeypatch.setattr(shared_state, "delete_state", store.delete)
    return store.entries


def skill_md(name: str, body: str = "Do the thing.", description: str = "Handles a thing.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n{body}\n"


def write_skill(instance_dir: Path, skill_id: str, files: dict[str, str] | None = None) -> Path:
    folder = instance_dir / "ai_gateway" / "skills" / skill_id
    for rel, text in (files or {"SKILL.md": skill_md(skill_id)}).items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    root = tmp_path / "my-app"
    (root / "ai_gateway").mkdir(parents=True)
    (root / "ai_gateway" / "config.yml").write_text("skills:\n  schema: ${var.catalog}.skills\n", encoding="utf-8")
    return root
