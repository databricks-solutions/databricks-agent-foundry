"""Tests for nested-brick ``--instance`` resolution.

Nested bricks live at ``instances/<app>/<subdir>/<sub>``.  Their CLIs used to
build ``instances/<subdir>/<app>`` instead, so every invocation failed with
"Instance directory not found", and the sub-instance directory was handed to
``load_brick_variables``, which keys off the basename and so could not find the
instance either.  Both halves are covered here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry.bricks import (
    iter_nested_sub_instances,
    resolve_sub_instances,
    sub_instance_dir,
)

SUBDIR = "data_domains"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repo with one app owning two data domains plus noise directories."""
    domains = tmp_path / "instances" / "my-app" / SUBDIR
    for name in ("alpha", "beta", "_build", ".hidden"):
        (domains / name).mkdir(parents=True)
    (tmp_path / "instances" / "other-app" / SUBDIR / "gamma").mkdir(parents=True)
    return tmp_path


class TestResolveByName:
    def test_app_name_returns_every_sub_instance(self, project: Path) -> None:
        instance_dir, subs = resolve_sub_instances(project, "my-app", SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["alpha", "beta"]

    def test_app_and_sub_pair_returns_just_that_one(self, project: Path) -> None:
        instance_dir, subs = resolve_sub_instances(project, "my-app/beta", SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["beta"]

    def test_app_subdir_sub_triple_is_accepted(self, project: Path) -> None:
        """The job brick documents ``<app>/<subdir>/<sub>``; keep the surface uniform."""
        instance_dir, subs = resolve_sub_instances(project, f"my-app/{SUBDIR}/alpha", SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["alpha"]

    def test_underscore_and_dot_directories_are_skipped(self, project: Path) -> None:
        _, subs = resolve_sub_instances(project, "my-app", SUBDIR)

        names = [s.name for s in subs]
        assert "_build" not in names
        assert ".hidden" not in names


class TestResolveByPath:
    def test_path_to_a_sub_instance(self, project: Path) -> None:
        supplied = str(project / "instances" / "my-app" / SUBDIR / "alpha")
        instance_dir, subs = resolve_sub_instances(project, supplied, SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["alpha"]

    def test_path_to_an_instance(self, project: Path) -> None:
        supplied = str(project / "instances" / "my-app")
        instance_dir, subs = resolve_sub_instances(project, supplied, SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["alpha", "beta"]

    def test_path_to_the_subdir_root(self, project: Path) -> None:
        supplied = str(project / "instances" / "my-app" / SUBDIR)
        instance_dir, subs = resolve_sub_instances(project, supplied, SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["alpha", "beta"]

    def test_relative_path_resolves_against_the_project_root(self, project: Path) -> None:
        instance_dir, subs = resolve_sub_instances(project, f"instances/my-app/{SUBDIR}/beta", SUBDIR)

        assert instance_dir == project / "instances" / "my-app"
        assert [s.name for s in subs] == ["beta"]


class TestFailures:
    def test_unknown_app(self, project: Path) -> None:
        with pytest.raises(ValueError, match="Instance directory not found"):
            resolve_sub_instances(project, "nope", SUBDIR)

    def test_app_without_the_subdir(self, project: Path) -> None:
        (project / "instances" / "bare").mkdir()
        with pytest.raises(ValueError, match=f"No {SUBDIR}/ directory"):
            resolve_sub_instances(project, "bare", SUBDIR)

    def test_unknown_sub_instance(self, project: Path) -> None:
        with pytest.raises(ValueError, match="Sub-instance not found"):
            resolve_sub_instances(project, "my-app/delta", SUBDIR)

    def test_empty_subdir(self, project: Path) -> None:
        (project / "instances" / "empty" / SUBDIR).mkdir(parents=True)
        with pytest.raises(ValueError, match="No sub-instances found"):
            resolve_sub_instances(project, "empty", SUBDIR)


class TestLayoutHelpers:
    def test_sub_instance_dir_needs_no_existing_directory(self, tmp_path: Path) -> None:
        path = sub_instance_dir(tmp_path, "my-app", "genie_spaces", "new_space")

        assert path == tmp_path / "instances" / "my-app" / "genie_spaces" / "new_space"
        assert not path.exists()

    def test_iter_nested_sub_instances_is_sorted(self, project: Path) -> None:
        subs = list(iter_nested_sub_instances(project / "instances" / "my-app", SUBDIR))

        assert [s.name for s in subs] == ["alpha", "beta"]

    def test_iter_nested_sub_instances_tolerates_a_missing_subdir(self, tmp_path: Path) -> None:
        assert list(iter_nested_sub_instances(tmp_path, SUBDIR)) == []
