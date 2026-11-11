"""Tests for agent_foundry.deploy.staging (issue #112).

The staging helper builds a self-contained payload for ``databricks
sync`` so the deployed Databricks App pulls runtime code from a
pip-installed wheel rather than from synced source files.  These tests
pin the contract on the layout, the ``[runtime]`` extra, and the
exclusion of dev caches.

The helper re-packs the currently-installed ``agent-foundry``
distribution as a wheel via ``zipfile`` (no external build tools
needed), so the suite runs anywhere ``agent-foundry`` itself is
importable.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import zipfile
from functools import partial
from pathlib import Path
from unittest import mock

import pytest

from agent_foundry.deploy import staging as staging_mod
from agent_foundry.deploy.staging import (
    _make_local_version_segment,
    _pack_installed_framework_wheel,
    _stamp_wheel_version,
    build_deploy_staging,
    clean_workspace_dir,
)


@pytest.fixture()
def synthetic_instance(tmp_path: Path) -> Path:
    """Minimal instance dir: just ``foundry.toml`` + a payload file."""
    inst = tmp_path / "my-app"
    inst.mkdir()
    (inst / "foundry.toml").write_text(
        'name = "my-app"\ndescription = "synthetic"\nschema_prefix = "my_app"\n',
        encoding="utf-8",
    )
    payload_dir = inst / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(
        "payload:\n  adapter: agent\n  spec:\n    runtime:\n"
        "      model:\n        kind: serving_endpoint\n        name: x\n",
        encoding="utf-8",
    )
    return inst


def _copy_wheel_into(wheel: Path, staging: Path) -> Path:
    return Path(shutil.copy2(wheel, staging))


@pytest.fixture(scope="module")
def framework_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real framework wheel, built once (``python -m build`` in a dev checkout)."""
    return _pack_installed_framework_wheel(tmp_path_factory.mktemp("framework-wheel"))


@pytest.fixture()
def reuse_framework_wheel(framework_wheel: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staging_mod, "_pack_installed_framework_wheel", partial(_copy_wheel_into, framework_wheel))


@pytest.mark.usefixtures("reuse_framework_wheel")
class TestBuildDeployStaging:
    """End-to-end shape of the staging dir produced for ``databricks sync``."""

    def test_staging_contains_wheel_requirements_and_instance(
        self,
        synthetic_instance: Path,
    ):
        handle = build_deploy_staging(synthetic_instance)
        try:
            staging = Path(handle.name)
            wheels = list(staging.glob("*.whl"))
            assert len(wheels) == 1, f"expected 1 wheel, got {[w.name for w in wheels]}"
            assert wheels[0].name.startswith("agent_foundry-")
            assert wheels[0].name.endswith("-py3-none-any.whl")

            req = staging / "requirements.txt"
            assert req.is_file()

            inst = staging / "instances" / "my-app"
            assert (inst / "foundry.toml").is_file()
            assert (inst / "payloads" / "agent" / "payload.yml").is_file()
        finally:
            handle.cleanup()

    def test_requirements_references_wheel_with_runtime_extra(
        self,
        synthetic_instance: Path,
    ):
        """The ``[runtime]`` extra is REQUIRED -- without it the App
        container's pip would install only base deps and the App would
        crash at startup on ``from agents import Runner``.  This test
        prevents the regression that the interim ``_strip_extra_marker``
        helper was fighting (see #112)."""
        handle = build_deploy_staging(synthetic_instance)
        try:
            staging = Path(handle.name)
            wheel_name = next(staging.glob("*.whl")).name
            req = (staging / "requirements.txt").read_text()
            assert f"./{wheel_name}[runtime]" in req, (
                f"requirements.txt missing wheel+runtime extra reference; got:\n{req}"
            )
            assert "-c constraints-runtime.txt" in req
            constraint = staging / "constraints-runtime.txt"
            assert constraint.is_file()
            text = constraint.read_text(encoding="utf-8")
            assert "openai-agents==" in text
            assert "--index-url" not in text
            assert "--hash=" not in text
            assert "pypi-proxy" not in text
        finally:
            handle.cleanup()

    def test_requirements_appends_extraction_extra(
        self,
        synthetic_instance: Path,
    ):
        """When the file-upload toggle is on, the Agent App brick passes
        ``extras=("runtime","extraction")`` so the App container also
        installs the extractor's deps (fastapi/uvicorn/openpyxl/...).
        Without this the extractor service cannot import and uploads fail.
        """
        handle = build_deploy_staging(
            synthetic_instance,
            extras=("runtime", "extraction"),
        )
        try:
            staging = Path(handle.name)
            wheel_name = next(staging.glob("*.whl")).name
            req = (staging / "requirements.txt").read_text()
            assert f"./{wheel_name}[runtime,extraction]" in req, (
                f"requirements.txt missing the extraction extra; got:\n{req}"
            )
            assert "-c constraints-extraction.txt" in req
            assert (staging / "constraints-extraction.txt").is_file()
        finally:
            handle.cleanup()

    def test_requirements_honors_rest_mcp_extra_without_runtime(
        self,
        synthetic_instance: Path,
    ):
        """The lean REST-to-MCP payload must not install agent dependencies."""
        handle = build_deploy_staging(synthetic_instance, extras=("rest_mcp",))
        try:
            staging = Path(handle.name)
            wheel_name = next(staging.glob("*.whl")).name
            req = (staging / "requirements.txt").read_text()
            assert f"./{wheel_name}[rest_mcp]" in req
            assert "-c constraints-rest_mcp.txt" in req
            assert "runtime" not in req
            assert (staging / "constraints-rest_mcp.txt").is_file()
            assert not (staging / "constraints-runtime.txt").exists()
        finally:
            handle.cleanup()

    def test_staging_does_not_ship_source_tree(
        self,
        synthetic_instance: Path,
    ):
        """The whole point of #112 -- no ``agent_foundry/`` /
        ``bricks/`` / ``pyproject.toml`` source files in the synced
        payload.  All runtime code rides inside the wheel."""
        handle = build_deploy_staging(synthetic_instance)
        try:
            staging = Path(handle.name)
            top_level = {p.name for p in staging.iterdir()}
            assert "agent_foundry" not in top_level
            assert "bricks" not in top_level
            assert "pyproject.toml" not in top_level
            assert "tests" not in top_level
            assert "docs" not in top_level
        finally:
            handle.cleanup()

    def test_copies_readme_to_staging_root(
        self,
        synthetic_instance: Path,
        tmp_path: Path,
    ):
        readme = tmp_path / "PAYLOAD_README.md"
        readme.write_text("# Payload\n", encoding="utf-8")
        handle = build_deploy_staging(synthetic_instance, readme=readme)
        try:
            assert (Path(handle.name) / "README.md").read_text(encoding="utf-8") == "# Payload\n"
        finally:
            handle.cleanup()

    def test_no_readme_when_not_given(
        self,
        synthetic_instance: Path,
    ):
        handle = build_deploy_staging(synthetic_instance)
        try:
            assert not (Path(handle.name) / "README.md").exists()
        finally:
            handle.cleanup()

    def test_excludes_state_and_build_caches_from_instance(
        self,
        synthetic_instance: Path,
    ):
        """``_state/``, ``_build/``, ``__pycache__/`` are deploy-time
        caches and must not bloat the synced instance dir."""
        (synthetic_instance / "_state").mkdir()
        (synthetic_instance / "_state" / "dev.json").write_text("{}")
        (synthetic_instance / "_build").mkdir()
        (synthetic_instance / "_build" / "junk.bin").write_text("x")
        (synthetic_instance / "__pycache__").mkdir()
        (synthetic_instance / "__pycache__" / "x.pyc").write_text("bin")

        handle = build_deploy_staging(synthetic_instance)
        try:
            inst = Path(handle.name) / "instances" / "my-app"
            assert not (inst / "_state").exists()
            assert not (inst / "_build").exists()
            assert not (inst / "__pycache__").exists()
            assert (inst / "foundry.toml").is_file(), "valid instance files survive"
        finally:
            handle.cleanup()

    def test_handle_cleanup_removes_staging_dir(
        self,
        synthetic_instance: Path,
    ):
        handle = build_deploy_staging(synthetic_instance)
        staging = Path(handle.name)
        assert staging.exists()
        handle.cleanup()
        assert not staging.exists()


class TestBuildDeployStagingErrorPaths:
    """Helper raises clear errors before the caller invests in syncing."""

    def test_missing_instance_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            build_deploy_staging(tmp_path / "no-such-instance")


class TestPackInstalledFrameworkWheel:
    """uv / editable installs often have no PEP 376 RECORD (issue #469)."""

    def test_missing_record_falls_back_to_source_build(self, tmp_path: Path) -> None:
        fake_dist = mock.MagicMock()
        fake_dist.read_text.return_value = None
        fake_dist.locate_file.return_value = tmp_path
        fake_dist.version = "0.1.0"
        sentinel = tmp_path / "agent_foundry-0.1.0-py3-none-any.whl"
        sentinel.write_bytes(b"wheel")

        with (
            mock.patch.object(staging_mod, "distribution", return_value=fake_dist),
            mock.patch.object(staging_mod, "_build_wheel_from_source", return_value=sentinel) as build,
        ):
            out = _pack_installed_framework_wheel(tmp_path)

        build.assert_called_once_with(tmp_path)
        assert out == sentinel

    def test_editable_pth_record_falls_back_to_source_build(self, tmp_path: Path) -> None:
        fake_dist = mock.MagicMock()
        fake_dist.read_text.return_value = "__editable__.agent_foundry-0.1.0.pth,\n"
        fake_dist.locate_file.return_value = tmp_path
        fake_dist.version = "0.1.0"
        sentinel = tmp_path / "agent_foundry-0.1.0-py3-none-any.whl"
        sentinel.write_bytes(b"wheel")

        with (
            mock.patch.object(staging_mod, "distribution", return_value=fake_dist),
            mock.patch.object(staging_mod, "_build_wheel_from_source", return_value=sentinel) as build,
        ):
            out = _pack_installed_framework_wheel(tmp_path)

        build.assert_called_once_with(tmp_path)
        assert out == sentinel


def _build_synthetic_wheel(tmp_path: Path, version: str = "0.1.0") -> Path:
    """Build a minimal but spec-conformant wheel for stamping tests.

    Avoids dependency on the installed ``agent-foundry`` distribution
    (which is editable in the dev workflow and does not have a
    standard RECORD).  Includes one package file plus the four
    ``dist-info`` files pip cares about.
    """
    dist_info = f"agent_foundry-{version}.dist-info"
    pkg_body = b"x = 1\n"
    metadata_text = f"Metadata-Version: 2.4\nName: agent-foundry\nVersion: {version}\nSummary: synthetic\n"
    wheel_body = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    top_level_body = "agent_foundry\n"

    def _digest(b: bytes) -> tuple[str, int]:
        d = base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode("ascii")
        return f"sha256={d}", len(b)

    pkg_h, pkg_n = _digest(pkg_body)
    meta_h, meta_n = _digest(metadata_text.encode())
    wheel_h, wheel_n = _digest(wheel_body.encode())
    top_h, top_n = _digest(top_level_body.encode())

    record = (
        "\n".join(
            [
                f"agent_foundry/__init__.py,{pkg_h},{pkg_n}",
                f"{dist_info}/METADATA,{meta_h},{meta_n}",
                f"{dist_info}/WHEEL,{wheel_h},{wheel_n}",
                f"{dist_info}/top_level.txt,{top_h},{top_n}",
                f"{dist_info}/RECORD,,",
            ]
        )
        + "\n"
    )

    out = tmp_path / f"agent_foundry-{version}-py3-none-any.whl"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent_foundry/__init__.py", pkg_body)
        zf.writestr(f"{dist_info}/METADATA", metadata_text)
        zf.writestr(f"{dist_info}/WHEEL", wheel_body)
        zf.writestr(f"{dist_info}/top_level.txt", top_level_body)
        zf.writestr(f"{dist_info}/RECORD", record)
    return out


class TestStampWheelVersion:
    """``_stamp_wheel_version`` rewrites the wheel so pip sees a fresh
    upgrade target on every deploy (issue #185).  Without this, warm
    Databricks App venvs short-circuit pip's "already satisfied" check
    and the new wheel's contents never land in ``site-packages``."""

    def test_stamps_filename_metadata_and_dist_info(self, tmp_path: Path):
        """All three version-bearing surfaces (filename, METADATA
        ``Version:`` header, dist-info dir name) carry the local
        segment after stamping."""
        wheel = _build_synthetic_wheel(tmp_path)
        out = _stamp_wheel_version(wheel, "abc123")

        assert out.name == "agent_foundry-0.1.0+abc123-py3-none-any.whl"
        assert not wheel.exists(), "old unstamped wheel must be removed"
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "agent_foundry-0.1.0+abc123.dist-info/METADATA" in names
            assert "agent_foundry-0.1.0.dist-info/METADATA" not in names
            meta = zf.read("agent_foundry-0.1.0+abc123.dist-info/METADATA").decode()
            assert "Version: 0.1.0+abc123\n" in meta
            assert "Version: 0.1.0\n" not in meta

    def test_record_is_internally_consistent(self, tmp_path: Path):
        """Every ``sha256=...`` and size in RECORD must match the actual
        zip entry contents.  pip refuses to install a wheel whose
        RECORD doesn't verify."""
        wheel = _build_synthetic_wheel(tmp_path)
        out = _stamp_wheel_version(wheel, "stampX")

        with zipfile.ZipFile(out) as zf:
            record = zf.read("agent_foundry-0.1.0+stampX.dist-info/RECORD").decode()
            for line in record.splitlines():
                rel, _, hash_size = line.partition(",")
                if not hash_size.startswith("sha256="):
                    continue
                expected_hash, _, expected_size_s = hash_size.partition(",")
                expected_size = int(expected_size_s)
                actual = zf.read(rel)
                assert len(actual) == expected_size, (
                    f"size mismatch for {rel}: RECORD says {expected_size}, actual {len(actual)}"
                )
                actual_b64 = base64.urlsafe_b64encode(hashlib.sha256(actual).digest()).rstrip(b"=").decode("ascii")
                assert expected_hash == f"sha256={actual_b64}", f"hash mismatch for {rel}"

    def test_record_self_row_has_empty_hash_and_size(self, tmp_path: Path):
        """PEP 376 requires the RECORD's own row to omit hash + size."""
        wheel = _build_synthetic_wheel(tmp_path)
        out = _stamp_wheel_version(wheel, "self")
        with zipfile.ZipFile(out) as zf:
            record = zf.read("agent_foundry-0.1.0+self.dist-info/RECORD").decode()
        assert "agent_foundry-0.1.0+self.dist-info/RECORD,,\n" in record + "\n"

    def test_two_stamps_produce_distinct_wheels(self, tmp_path: Path):
        """The whole point of stamping is uniqueness across deploys."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        w1 = _build_synthetic_wheel(dir_a)
        w2 = _build_synthetic_wheel(dir_b)
        out1 = _stamp_wheel_version(w1, "111")
        out2 = _stamp_wheel_version(w2, "222")
        assert out1.name != out2.name
        assert out1.name.endswith("+111-py3-none-any.whl")
        assert out2.name.endswith("+222-py3-none-any.whl")

    def test_pip_metadata_view_matches_stamp(self, tmp_path: Path):
        """Read the stamped wheel back via ``email.parser`` (same
        machinery pip uses to parse METADATA) and confirm the parsed
        ``Version`` field carries the local segment."""
        from email.parser import Parser

        wheel = _build_synthetic_wheel(tmp_path)
        out = _stamp_wheel_version(wheel, "deploy42")
        with zipfile.ZipFile(out) as zf:
            meta_text = zf.read("agent_foundry-0.1.0+deploy42.dist-info/METADATA").decode()
        msg = Parser().parsestr(meta_text)
        assert msg["Version"] == "0.1.0+deploy42"

    def test_make_local_version_segment_is_pep440_compatible(self):
        """The default stamp must satisfy PEP 440's local-segment grammar
        (``[a-zA-Z0-9.]+``)."""
        import re

        seg = _make_local_version_segment()
        assert re.fullmatch(r"[a-zA-Z0-9.]+", seg), seg


class TestCleanWorkspaceDir:
    """``databricks sync --full`` does NOT delete absent files
    (https://github.com/databricks/databricks-cli/issues/688), so the
    deploy scripts call ``clean_workspace_dir`` first to wipe the
    target.  These tests pin that behaviour against a mocked
    ``WorkspaceClient``."""

    def test_dry_run_does_not_call_sdk(self):
        ws = mock.MagicMock()
        clean_workspace_dir(ws, "/Users/x/app", dry_run=True)
        ws.workspace.delete.assert_not_called()

    def test_real_run_deletes_recursively(self):
        ws = mock.MagicMock()
        assert clean_workspace_dir(ws, "/Users/x/app", dry_run=False) is True
        ws.workspace.delete.assert_called_once_with("/Users/x/app", recursive=True)

    def test_not_found_is_tolerated(self, capsys: pytest.CaptureFixture):
        """Legacy first-ever deploy: bare Exception with NOT_FOUND-ish
        message must not abort.  Kept as belt-and-braces for SDK
        wrappers that raise ``Exception`` instead of the typed class."""
        ws = mock.MagicMock()
        ws.workspace.delete.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        clean_workspace_dir(ws, "/Users/x/app", dry_run=False)
        assert "does not exist -- nothing to clean" in capsys.readouterr().out

    def test_typed_not_found_is_tolerated(self, capsys: pytest.CaptureFixture):
        """First-ever deploy with a current SDK: ``ws.workspace.delete``
        raises the typed :class:`databricks.sdk.errors.NotFound` whose
        ``str(exc)`` is the bare ``Path (...) doesn't exist.`` message
        -- substring matching alone would let it propagate and abort
        the deploy.  Regression guard for the first-deploy bug fixed
        alongside this test."""
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        ws.workspace.delete.side_effect = NotFound("Path (/Shared/agent-foundry/dev/test-agent/source) doesn't exist.")
        assert clean_workspace_dir(ws, "/Shared/agent-foundry/dev/test-agent/source", dry_run=False) is False
        assert "does not exist -- nothing to clean" in capsys.readouterr().out

    def test_other_errors_propagate(self):
        """Permission errors etc. MUST surface so the operator sees them."""
        ws = mock.MagicMock()
        ws.workspace.delete.side_effect = Exception("PERMISSION_DENIED")
        with pytest.raises(Exception, match="PERMISSION_DENIED"):
            clean_workspace_dir(ws, "/Users/x/app", dry_run=False)
