"""Guards for the pytest11 skip gate (#434)."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import cast

import pytest

from agent_foundry.env import AGENT_FOUNDRY_REQUIRE_LIVE, AGENT_FOUNDRY_TELEMETRY_DISABLED
from agent_foundry.pytest_plugin import (
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_configure,
    require_uv_install,
    strip_ansi,
)
from agent_foundry.testing.live import is_live_path
from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _pytester_asyncio_scope(request: pytest.FixtureRequest) -> None:
    """Inner pytest runs load pytest-asyncio but not this repo's pyproject.toml."""
    if "pytester" in request.fixturenames:
        request.getfixturevalue("pytester").makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")


class _Parser:
    def __init__(self) -> None:
        self.options: list[str] = []

    def addoption(self, name: str, **kwargs: object) -> None:
        self.options.append(name)


class _Config:
    def __init__(self, *, live: bool = False, component: bool = False, gone: bool = False) -> None:
        self._flags = {
            "--run-live": live,
            "--run-component": component,
            "--run-gone": gone,
        }
        self.ini_lines: list[tuple[str, str]] = []

    def getoption(self, name: str, default: bool = False, **_kwargs: object) -> bool:
        if name not in self._flags:
            raise AssertionError(f"unexpected option {name}")
        return self._flags[name]

    def addinivalue_line(self, name: str, line: str) -> None:
        self.ini_lines.append((name, line))


class _Item:
    def __init__(self, *markers: str, path: str = "pkg/tests/unit/test_x.py") -> None:
        self._markers = set(markers)
        self.path = Path(path)
        self.added: list[object] = []

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None

    def add_marker(self, mark: object) -> None:
        self.added.append(mark)
        if isinstance(mark, str):
            self._markers.add(mark)


def _skipped(item: _Item) -> bool:
    return any(getattr(mark, "name", None) == "skip" for mark in item.added)


def test_disables_telemetry_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENT_FOUNDRY_TELEMETRY_DISABLED.name, raising=False)
    monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)
    pytest_configure(_Config())  # type: ignore[arg-type]
    assert os.environ[AGENT_FOUNDRY_TELEMETRY_DISABLED.name] == "true"
    assert os.environ[SKIP_DEFAULT_WORKSPACE_CLIENT_ENV] == "true"


def test_run_live_clears_inherited_skip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    pytest_configure(_Config(live=True))  # type: ignore[arg-type]
    assert SKIP_DEFAULT_WORKSPACE_CLIENT_ENV not in os.environ


def test_configure_registers_markers() -> None:
    config = _Config()
    pytest_configure(config)  # type: ignore[arg-type]
    names = sorted(line.split(":", 1)[0] for kind, line in config.ini_lines if kind == "markers")
    assert names == ["component", "gone", "live"]


def test_does_not_override_explicit_telemetry_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_FOUNDRY_TELEMETRY_DISABLED.name, "false")
    pytest_configure(_Config())  # type: ignore[arg-type]
    assert os.environ[AGENT_FOUNDRY_TELEMETRY_DISABLED.name] == "false"


def test_strip_ansi_rejoins_rich_flag_dashes() -> None:
    assert "--instance" in strip_ansi("\x1b[1m-\x1b[0m\x1b[1m-instance")


def test_registers_cli_flags() -> None:
    parser = _Parser()
    pytest_addoption(parser)  # type: ignore[arg-type]
    assert parser.options == ["--run-live", "--run-component", "--run-gone"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("bricks/data/tests/live/test_views.py", True),
        ("instances/x/tests/live/sub/test_app.py", True),
        ("bricks/data/tests/unit/test_views.py", False),
        ("tests/contracts/test_live.py", False),
        ("live/tests/test_x.py", False),
    ],
)
def test_is_live_path(path: str, expected: bool) -> None:
    assert is_live_path(Path(path)) is expected


def test_skips_marked_tests_when_flags_are_off() -> None:
    folder_live = _Item(path="bricks/data/tests/live/test_views.py")
    component = _Item("component")
    unit = _Item()
    pytest_collection_modifyitems(
        _Config(),  # type: ignore[arg-type]
        cast(list[pytest.Item], [folder_live, component, unit]),
    )
    assert "live" in folder_live.added
    assert _skipped(folder_live)
    assert _skipped(component)
    assert not unit.added


def test_gone_checks_need_their_own_flag() -> None:
    """A deployed-phase ``--run-live`` run skips the post-destroy checks."""
    gone = _Item("gone", path="bricks/data/tests/live/test_gone.py")
    pytest_collection_modifyitems(_Config(live=True), cast(list[pytest.Item], [gone]))  # type: ignore[arg-type]
    assert _skipped(gone)


def test_does_not_skip_when_flags_are_on() -> None:
    folder_live = _Item(path="bricks/data/tests/live/test_views.py")
    component = _Item("component")
    pytest_collection_modifyitems(
        _Config(live=True, component=True),  # type: ignore[arg-type]
        cast(list[pytest.Item], [folder_live, component]),
    )
    assert folder_live.added == ["live"]
    assert not component.added


def test_entry_point_is_declared() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    plugin = pyproject["project"]["entry-points"]["pytest11"]["agent_foundry"]
    assert plugin == "agent_foundry.pytest_plugin"


def test_pytest_help_lists_flags(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest("--help")
    assert result.ret == 0
    stdout = result.stdout.str()
    assert "--run-live" in stdout
    assert "--run-component" in stdout
    assert "--run-gone" in stdout
    assert "--run-integration" not in stdout
    assert "--run-acceptance" not in stdout
    assert "--run-local-agent" not in stdout


def test_deploy_instance_dry_run_under_skip_env_does_not_construct_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant: skip-env + dry-run must never call the SDK factory."""
    from agent_foundry.deploy._orchestrator import deploy_instance
    from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    constructed: list[object] = []

    def _boom(*_a: object, **_k: object) -> None:
        constructed.append(1)
        raise AssertionError("get_workspace_client must not run under the skip env")

    monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)
    monkeypatch.setattr(
        "agent_foundry.payload_requirements.enforce_payload_requirements",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("agent_foundry.deploy._orchestrator.iter_bricks", lambda: [])

    deploy_instance(tmp_path, "dev", variables={"app_name": "x"}, dry_run=True)
    assert constructed == []


def test_preload_state_does_not_construct_default_client(tmp_path: Path) -> None:
    """Regression: unmocked ``_try_get_workspace_client`` hung CI for minutes."""
    import time

    from agent_foundry.deploy._orchestrator import _preload_state

    started = time.monotonic()
    _preload_state(tmp_path, "dev", {}, dry_run=True)
    assert time.monotonic() - started < 2.0


def _make_live_file(pytester: pytest.Pytester, body: str) -> None:
    """``owner/tests/live/test_deployed.py``; a unique top package avoids the repo's ``tests``."""
    live_dir = pytester.path / "owner" / "tests" / "live"
    live_dir.mkdir(parents=True)
    for pkg in (live_dir.parents[1], live_dir.parent, live_dir):
        pkg.joinpath("__init__.py").write_text("", encoding="utf-8")
    live_dir.joinpath("test_deployed.py").write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_skip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process ``--run-live`` runs pop the skip env; monkeypatch puts it back afterwards."""
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, os.environ.get(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true"))


def test_run_live_clears_skip_env_in_real_pytest(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    _make_live_file(
        pytester,
        "from agent_foundry.workspace_client import skip_default_workspace_client\n\n"
        "def test_skip_cleared():\n"
        "    assert not skip_default_workspace_client()\n",
    )
    result = pytester.runpytest("--run-live", "-q")
    result.assert_outcomes(passed=1)


def test_unit_run_skips_live_and_component(pytester: pytest.Pytester) -> None:
    """Markers come from the plugin; tests/live/ is live without a pytestmark."""
    pytester.makepyfile(
        """
        import pytest

        def test_unit():
            assert True

        @pytest.mark.component
        def test_component():
            raise AssertionError("unit run must skip component")
        """
    )
    _make_live_file(pytester, 'def test_deployed():\n    raise AssertionError("unit run must skip live")\n')
    result = pytester.runpytest("-q", "--strict-markers")
    result.assert_outcomes(passed=1, skipped=2)


def test_select_live_sees_folder_marker(pytester: pytest.Pytester) -> None:
    """``-m live`` deselection runs after the folder marker is applied."""
    pytester.makepyfile("def test_unit():\n    raise AssertionError('deselected')\n")
    _make_live_file(pytester, "def test_deployed():\n    assert True\n")
    result = pytester.runpytest("-q", "-m", "live", "--run-live")
    result.assert_outcomes(passed=1, deselected=1)


def test_live_target_skips_without_binding(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv(AGENT_FOUNDRY_REQUIRE_LIVE.name, raising=False)
    _make_live_file(pytester, "def test_deployed(live_target):\n    raise AssertionError('no binding')\n")
    result = pytester.runpytest("-q", "--run-live")
    result.assert_outcomes(skipped=1)


def test_live_target_fails_without_binding_when_required(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.setenv(AGENT_FOUNDRY_REQUIRE_LIVE.name, "1")
    _make_live_file(pytester, "def test_deployed(live_target):\n    raise AssertionError('no binding')\n")
    result = pytester.runpytest("-q", "--run-live")
    result.assert_outcomes(errors=1)


def test_gone_variables_replay_what_the_deployed_run_recorded(pytester: pytest.Pytester) -> None:
    """``destroy`` deletes state; the gone phase reads outputs cached by the deployed phase."""
    pytester.makeconftest(
        """
        from pathlib import Path

        import pytest

        import agent_foundry.testing.live as live
        from agent_foundry.testing.workspace import WorkspaceBinding

        live.load_live_variables = lambda target, ws: {"warehouse_id": "w-1"}

        @pytest.fixture(scope="session")
        def live_target():
            binding = WorkspaceBinding(host="https://h.cloud.databricks.com", catalog="c")
            return live.LiveTarget(binding=binding, project_root=Path("."), instance="inst")

        @pytest.fixture(scope="session")
        def live_ws():
            return object()
        """
    )
    _make_live_file(
        pytester,
        "import pytest\n\n"
        "def test_deployed(live_variables):\n"
        "    assert live_variables['warehouse_id'] == 'w-1'\n\n"
        "@pytest.mark.gone\n"
        "def test_destroyed(gone_variables):\n"
        "    assert gone_variables == {'warehouse_id': 'w-1'}\n",
    )
    pytester.runpytest_subprocess("-q", "--run-live").assert_outcomes(passed=1, skipped=1)
    pytester.runpytest_subprocess("-q", "-m", "gone", "--run-live", "--run-gone").assert_outcomes(
        passed=1, deselected=1
    )


def test_gone_variables_fail_when_nothing_was_recorded(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AGENT_FOUNDRY_REQUIRE_LIVE.name, "1")
    pytester.makeconftest(
        """
        from pathlib import Path

        import pytest

        from agent_foundry.testing.live import LiveTarget
        from agent_foundry.testing.workspace import WorkspaceBinding

        @pytest.fixture(scope="session")
        def live_target():
            binding = WorkspaceBinding(host="https://h.cloud.databricks.com", catalog="c")
            return LiveTarget(binding=binding, project_root=Path("."), instance="inst")
        """
    )
    _make_live_file(pytester, "def test_destroyed(gone_variables):\n    pass\n")
    pytester.runpytest("-q", "--run-live", "-p", "no:cacheprovider").assert_outcomes(errors=1)


def test_require_uv_install_skips_offline_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    install = subprocess.CompletedProcess(
        args=["uv"],
        returncode=1,
        stdout="",
        stderr="network was disabled",
    )
    with pytest.raises(pytest.skip.Exception, match="pypi/PyPI"):
        require_uv_install(install, "uv pip install .")


def test_require_uv_install_fails_offline_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    install = subprocess.CompletedProcess(
        args=["uv"],
        returncode=1,
        stdout="",
        stderr="Failed to fetch PyPI",
    )
    with pytest.raises(pytest.fail.Exception, match="pypi/PyPI"):
        require_uv_install(install, "uv pip install .")


def test_require_uv_install_skips_unsatisfiable_pin_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    install = subprocess.CompletedProcess(
        args=["uv"],
        returncode=1,
        stdout="",
        stderr="No solution found when resolving dependencies:\nBecause there is no version of litellm==1.88.6",
    )
    with pytest.raises(pytest.skip.Exception, match="pypi/PyPI"):
        require_uv_install(install, "uv pip install .[runtime]")


def test_uv_pip_install_cmd_forwards_default_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_foundry.pytest_plugin import uv_pip_install_cmd

    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://example.invalid/simple")
    cmd = uv_pip_install_cmd("/tmp/python", "/tmp/proj")
    assert cmd[-2:] == ["--default-index", "https://example.invalid/simple"]
