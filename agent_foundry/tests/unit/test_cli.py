"""Smoke tests for ``agent-foundry`` typer CLI (issue #96).

Coverage strategy:

- Every public subcommand has a ``--help`` smoke that asserts exit 0
  and the expected description.
- Each subcommand has at least one happy-path test: the underlying
  library function is mocked so the test is fast and deterministic
  (no Databricks workspace, no subprocesses).
- Targeted error-path tests for missing required flags and unknown
  brick / unknown action.

The CliRunner uses typer's standalone-mode-off semantics so command
exits surface as ``result.exit_code`` instead of ``SystemExit``.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import Result
from typer.testing import CliRunner

from agent_foundry.deploy.cli import app
from agent_foundry.deploy.cli import main as cli_main
from agent_foundry.pytest_plugin import strip_ansi
from agent_foundry.smoke import SmokeProbe, SmokeTarget

# Click >= 8.2 keeps stdout/stderr separate by default and removed the
# ``mix_stderr`` kwarg. ``click>=8.1`` still allows 8.1, where we need to
# opt in explicitly, otherwise ``result.stderr`` raises ValueError.
_CLICK_MAJOR, _CLICK_MINOR, *_ = (int(p) for p in click.__version__.split(".")[:2])
_CLI_RUNNER_KWARGS: dict = {} if (_CLICK_MAJOR, _CLICK_MINOR) >= (8, 2) else {"mix_stderr": False}


def combined(result: Result) -> str:
    """Return stdout + stderr as a single string, on any click version.

    Tests sometimes assert against output the CLI writes to stderr
    (e.g. warnings emitted via ``typer.echo(..., err=True)``). On click
    8.1 with ``mix_stderr=False`` and on click 8.2+ those streams are
    split, so a plain ``result.output`` check would miss them.
    """
    stdout = result.stdout or ""
    try:
        stderr = result.stderr or ""
    except ValueError:
        stderr = ""
    return strip_ansi(stdout + stderr)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(**_CLI_RUNNER_KWARGS)


# ---------------------------------------------------------------------------
# --help smoke per subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["deploy", "--help"],
        ["validate", "--help"],
        ["eval", "--help"],
        ["test", "--help"],
        ["run", "--help"],
        ["repl", "--help"],
        ["brick", "--help"],
        ["destroy", "--help"],
        ["wait", "--help"],
        ["smoke", "--help"],
        ["monitors", "--help"],
        ["monitors", "start", "--help"],
        ["monitors", "stop", "--help"],
        ["monitors", "status", "--help"],
        ["monitors", "delete", "--help"],
        ["call", "--help"],
        ["call", "a2a", "--help"],
        ["call", "mcp", "--help"],
        ["genie", "--help"],
        ["genie", "import", "--help"],
    ],
)
def test_help_smoke(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, f"agent-foundry {' '.join(argv)} failed: {result.stdout}{result.stderr}"
    assert "Usage:" in result.stdout


# ---------------------------------------------------------------------------
# main(argv) -> int contract
# ---------------------------------------------------------------------------


def test_main_returns_int_for_help() -> None:
    """The console-script entry point keeps the historical ``-> int``
    contract so existing callers (CI workflows, pytest harnesses) stay green."""
    rc = cli_main(["--help"])
    assert rc == 0


def test_main_returns_nonzero_for_unknown_command() -> None:
    rc = cli_main(["definitely-not-a-real-command"])
    assert rc != 0


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


def test_deploy_calls_run_deploy_with_parsed_flags(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_deploy(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy._run_deploy", _fake_run_deploy)

    result = runner.invoke(
        app,
        [
            "deploy",
            "--instance",
            "my-app",
            "--target",
            "staging",
            "--dry-run",
            "--skip",
            "agent_app,observability",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["instance"] == "my-app"
    assert captured["target"] == "staging"
    assert captured["dry_run"] is True
    assert captured["skip"] == "agent_app,observability"
    assert captured["only"] == ""
    assert captured["offline"] is False


def test_deploy_offline_requires_dry_run(runner: CliRunner) -> None:
    result = runner.invoke(app, ["deploy", "--instance", "my-app", "--offline"])
    assert result.exit_code == 1, combined(result)
    assert "--offline requires --dry-run" in combined(result)


def test_deploy_passes_offline_with_dry_run(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_deploy(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy._run_deploy", _fake_run_deploy)
    result = runner.invoke(app, ["deploy", "--instance", "my-app", "--dry-run", "--offline"])
    assert result.exit_code == 0, combined(result)
    assert captured["offline"] is True
    assert captured["dry_run"] is True


def test_deploy_passes_only_flag(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_deploy(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy._run_deploy", _fake_run_deploy)

    result = runner.invoke(
        app,
        ["deploy", "--instance", "my-app", "--only", "warehouse,skills"],
    )
    assert result.exit_code == 0, combined(result)
    assert captured["only"] == "warehouse,skills"
    assert captured["skip"] == ""


def test_deploy_skip_and_only_are_mutually_exclusive(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["deploy", "--instance", "my-app", "--skip", "warehouse", "--only", "skills"],
    )
    assert result.exit_code == 1, combined(result)
    assert "--skip and --only are mutually exclusive" in combined(result)


def test_deploy_only_unknown_brick_errors(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["deploy", "--instance", "my-app", "--only", "bogus-typo"],
    )
    out = combined(result)
    assert result.exit_code == 1, out
    assert "--only matched no brick for: bogus-typo" in out
    # The hint lists one canonical CLI handle per brick, not every
    # display/registry/alias spelling (regression guard for the noisy
    # 21-entry dump).
    assert "app" in out
    assert "mlflow experiment" not in out


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_calls_run_checks(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_checks(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("agent_foundry.validate.run_checks", _fake_run_checks)

    result = runner.invoke(app, ["validate", "--workspace", "--target", "staging"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["workspace"] is True
    assert captured["target"] == "staging"


# ---------------------------------------------------------------------------
# test -- pytest subprocess env (#432)
# ---------------------------------------------------------------------------


def _write_foundry_toml(root: Path, *, profile: str, host: str) -> None:
    (root / "foundry.toml").write_text(
        "schema_version = 2\n\n"
        "[targets.dev]\n"
        f'host = "{host}"\n'
        'catalog = "cat"\n'
        f'profile = "{profile}"\n\n'
        '[[instances]]\npath = "instances/demo"\n',
        encoding="utf-8",
    )


def test_test_cmd_injects_profile_and_project_root(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pytest child must not fall back to [DEFAULT] auth (#432)."""
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    captured: dict[str, object] = {}

    def _fake_run(cmd: object, env: dict[str, str] | None = None, **kwargs: object) -> object:
        captured["env"] = env
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("agent_foundry.deploy.cli.verify.subprocess.run", _fake_run)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("AGENT_FOUNDRY_PROJECT_ROOT", raising=False)

    result = runner.invoke(app, ["test", "--target", "dev"])
    assert result.exit_code == 0, combined(result)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DATABRICKS_CONFIG_PROFILE"] == "dev-profile"
    assert env["DATABRICKS_HOST"] == "https://ws.example"
    assert env["AGENT_FOUNDRY_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert env["AGENT_FOUNDRY_TARGET"] == "dev"


def test_test_cmd_offline_runs_pytest_with_offline_selector(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    captured: dict[str, object] = {}

    def _fake_run(cmd: object, env: dict[str, str] | None = None, **kwargs: object) -> object:
        captured["cmd"] = cmd
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("agent_foundry.deploy.cli.verify.subprocess.run", _fake_run)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["test", "--offline"])
    assert result.exit_code == 0, combined(result)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[1:3] == ["-m", "pytest"]
    assert cmd[-2:] == ["-m", "not live and not component"]


def test_run_tests_exports_profile_and_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[dict[str, str]] = []

    def _fake_run(cmd: object, cwd: object = None, env: dict[str, str] | None = None, **kwargs: object) -> object:
        assert env is not None
        captured.append(env)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy.subprocess.run", _fake_run)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_FOUNDRY_PROJECT_ROOT", raising=False)

    from agent_foundry.deploy.cli.deploy import _run_tests

    rc = _run_tests(
        tmp_path,
        {"host": "https://h.example", "warehouse_id": "abc", "profile": "dev-profile"},
        run_benchmarks=False,
        target="staging",
    )
    assert rc == 0
    assert len(captured) == 2
    for env in captured:
        assert env["DATABRICKS_CONFIG_PROFILE"] == "dev-profile"
        assert env["DATABRICKS_HOST"] == "https://h.example"
        assert env["TEST_WAREHOUSE_ID"] == "abc"
        assert env["AGENT_FOUNDRY_PROJECT_ROOT"] == str(tmp_path.resolve())
        assert env["AGENT_FOUNDRY_TARGET"] == "staging"


# ---------------------------------------------------------------------------
# wait -- App readiness between deploy and acceptance
# ---------------------------------------------------------------------------


class _FakeInstanceConfig:
    app_resource_name = "demo-app"


def _patch_wait_deps(monkeypatch: pytest.MonkeyPatch, wait_for_app: object) -> dict[str, object]:
    captured: dict[str, object] = {}

    def _load(repo_root: Path, *, instance: str, target_name: str) -> _FakeInstanceConfig:
        captured["repo_root"] = repo_root
        captured["instance"] = instance
        captured["target"] = target_name
        return _FakeInstanceConfig()

    def _client(variables: dict[str, str]) -> object:
        captured["variables"] = variables
        return object()

    monkeypatch.setattr("agent_foundry.loader.load_instance_config", _load)
    monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _client)
    monkeypatch.setattr("agent_foundry.testing.readiness.wait_for_app", wait_for_app)
    return captured


def _ready(ws: object, app_name: str, *, timeout_seconds: float) -> str:
    assert app_name == "demo-app"
    assert timeout_seconds == 30
    return "https://demo-app.example"


def _never_ready(ws: object, app_name: str, *, timeout_seconds: float) -> str:
    from agent_foundry.testing.readiness import ReadinessError

    raise ReadinessError("app 'demo-app' not ready after 30s")


def test_wait_prints_url_using_target_auth(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    captured = _patch_wait_deps(monkeypatch, _ready)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["wait", "--instance", "demo", "--target", "dev", "--timeout-seconds", "30"])

    assert result.exit_code == 0, combined(result)
    assert "https://demo-app.example" in result.stdout
    assert captured["repo_root"] == tmp_path
    assert captured["instance"] == "demo"
    assert captured["target"] == "dev"
    assert captured["variables"] == {"profile": "dev-profile", "host": "https://ws.example"}


def test_wait_exits_nonzero_when_app_not_ready(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    _patch_wait_deps(monkeypatch, _never_ready)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["wait", "--instance", "demo", "--timeout-seconds", "30"])

    assert result.exit_code == 1, combined(result)
    assert "not ready after 30s" in combined(result)


# ---------------------------------------------------------------------------
# smoke -- black-box probes of the deployed App
# ---------------------------------------------------------------------------


class _SmokeAdapter:
    name = "fake"

    def __init__(self, outcomes: dict[str, bool]) -> None:
        self.outcomes = outcomes
        self.targets: list[SmokeTarget] = []

    def smoke_probes(self, target: SmokeTarget) -> list[SmokeProbe]:
        self.targets.append(target)
        return [SmokeProbe(name, lambda ok=ok: _probe_outcome(ok)) for name, ok in self.outcomes.items()]


def _probe_outcome(ok: bool) -> str:
    if not ok:
        raise RuntimeError("boom")
    return "answered"


class _SmokeWs:
    def __init__(self, url: str) -> None:
        self.apps = self
        self.config = type("Cfg", (), {"host": "ws.example"})()
        self._url = url

    def get(self, name: str) -> object:
        assert name == "demo-app"
        return type("App", (), {"url": self._url})()


def _patch_smoke_deps(
    monkeypatch: pytest.MonkeyPatch, adapter: _SmokeAdapter, *, app_url: str = "https://demo.apps/x"
) -> None:
    monkeypatch.setattr("agent_foundry.loader.load_instance_config", lambda *_a, **_k: _FakeInstanceConfig())
    monkeypatch.setattr(
        "agent_foundry.deploy.payload_adapters.load_payload_config",
        lambda _d: type("P", (), {"adapter": "fake"})(),
    )
    monkeypatch.setattr("agent_foundry.deploy.payload_adapters.get_payload_adapter", lambda _n: adapter)
    monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", lambda _v: _SmokeWs(app_url))


def test_smoke_passes_and_targets_app_origin(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    adapter = _SmokeAdapter({"invocations": True, "mcp": True})
    _patch_smoke_deps(monkeypatch, adapter)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["smoke", "--instance", "demo"])

    assert result.exit_code == 0, combined(result)
    assert "PASS invocations: answered" in result.stdout
    assert "2/2 smoke probes passed" in result.stdout
    (target,) = adapter.targets
    assert target.app_url == "https://demo.apps"
    assert target.workspace_host == "https://ws.example"


def test_smoke_runs_every_probe_and_fails_on_any(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    _patch_smoke_deps(monkeypatch, _SmokeAdapter({"invocations": False, "mcp": True}))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["smoke", "--instance", "demo"])

    assert result.exit_code == 1, combined(result)
    assert "FAIL invocations: RuntimeError: boom" in result.stdout
    assert "PASS mcp: answered" in result.stdout


def test_smoke_fails_when_app_has_no_url(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_foundry_toml(tmp_path, profile="dev-profile", host="https://ws.example")
    _patch_smoke_deps(monkeypatch, _SmokeAdapter({"invocations": True}), app_url="")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["smoke", "--instance", "demo"])

    assert result.exit_code == 1, combined(result)
    assert "has no URL" in result.stdout


# ---------------------------------------------------------------------------
# eval / monitors -- forward to bricks.evaluation.scripts
# ---------------------------------------------------------------------------


def test_eval_forwards_argv(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_eval_main(argv: list[str] | None = None) -> int:
        captured["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(
        "bricks.evaluation.scripts.run_evaluation.main",
        _fake_eval_main,
    )

    result = runner.invoke(
        app,
        [
            "eval",
            "--instance",
            "demo",
            "--mode",
            "traces",
            "--max-traces",
            "5",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "--instance" in captured["argv"] and "demo" in captured["argv"]
    assert "--mode" in captured["argv"] and "traces" in captured["argv"]
    assert "--max-traces" in captured["argv"] and "5" in captured["argv"]


@pytest.mark.parametrize("action", ["start", "stop", "status", "delete"])
def test_monitors_actions_dispatch(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_register_main(argv: list[str] | None = None) -> int:
        captured["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(
        "bricks.evaluation.scripts.register_monitors.main",
        _fake_register_main,
    )

    result = runner.invoke(app, ["monitors", action, "--instance", "demo"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "--action" in captured["argv"] and action in captured["argv"]


# ---------------------------------------------------------------------------
# brick dispatch
# ---------------------------------------------------------------------------


def test_brick_dispatch_happy_path(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_action(argv: list[str] | None = None) -> int:
        captured["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(
        "agent_foundry.deploy._brick_registry.resolve_action",
        lambda brick, action: _fake_action,
    )

    result = runner.invoke(
        app,
        [
            "brick",
            "warehouse",
            "deploy",
            "--instance",
            "demo",
            "--target",
            "dev",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["argv"] == ["--instance", "demo", "--target", "dev"]


def test_brick_dispatch_unknown_brick_returns_2(runner: CliRunner) -> None:
    result = runner.invoke(app, ["brick", "not-a-brick", "deploy"])
    assert result.exit_code == 2
    assert "Unknown brick" in (result.stderr or result.stdout)


def test_brick_dispatch_unknown_action_returns_2(runner: CliRunner) -> None:
    result = runner.invoke(app, ["brick", "warehouse", "not-an-action"])
    assert result.exit_code == 2
    assert "Unknown action" in (result.stderr or result.stdout)


# ---------------------------------------------------------------------------
# call -- error + happy paths
# ---------------------------------------------------------------------------


def test_call_a2a_requires_instance_or_app_host(runner: CliRunner) -> None:
    result = runner.invoke(app, ["call", "a2a", "hello"])
    assert result.exit_code == 2
    out = combined(result)
    assert "--instance" in out and "--app-host" in out


def test_call_mcp_requires_instance_or_app_host(runner: CliRunner) -> None:
    result = runner.invoke(app, ["call", "mcp", "hello"])
    assert result.exit_code == 2
    out = combined(result)
    assert "--instance" in out and "--app-host" in out


def test_call_a2a_happy_path_with_app_host(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_call_a2a(*, app_host: str, profile: str | None, message: str) -> int:
        captured.update({"app_host": app_host, "profile": profile, "message": message})
        return 0

    monkeypatch.setattr("agent_foundry.cli.call.call_a2a", _fake_call_a2a)

    result = runner.invoke(
        app,
        [
            "call",
            "a2a",
            "--app-host",
            "demo.databricks.app",
            "--profile",
            "demo",
            "what is the answer?",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured == {
        "app_host": "demo.databricks.app",
        "profile": "demo",
        "message": "what is the answer?",
    }


def test_call_mcp_resolves_host_from_instance(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--instance`` resolves the host via resolve_app_host without requiring --app-host."""
    captured: dict[str, object] = {}

    def _fake_resolve(
        *,
        instance: str | None,
        target: str,
        profile: str | None,
        app_host: str | None,
    ) -> tuple[str, str | None]:
        captured.update(
            {
                "instance": instance,
                "target": target,
                "profile": profile,
                "app_host": app_host,
            }
        )
        return "resolved-host.databricksapps.com", "resolved-profile"

    def _fake_call_mcp(*, app_host: str, profile: str | None, message: str) -> int:
        captured["dispatched"] = (app_host, profile, message)
        return 0

    monkeypatch.setattr("agent_foundry.cli.call.resolve_app_host", _fake_resolve)
    monkeypatch.setattr("agent_foundry.cli.call.call_mcp", _fake_call_mcp)

    result = runner.invoke(
        app,
        [
            "call",
            "mcp",
            "--instance",
            "demo",
            "--target",
            "staging",
            "what is the answer?",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["instance"] == "demo"
    assert captured["target"] == "staging"
    assert captured["app_host"] is None
    assert captured["dispatched"] == (
        "resolved-host.databricksapps.com",
        "resolved-profile",
        "what is the answer?",
    )


def test_call_warns_when_both_instance_and_app_host_given(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_foundry.cli.call.call_mcp",
        lambda *, app_host, profile, message: 0,
    )

    result = runner.invoke(
        app,
        [
            "call",
            "mcp",
            "--instance",
            "demo",
            "--app-host",
            "explicit.databricksapps.com",
            "hello",
        ],
    )
    assert result.exit_code == 0, combined(result)
    # Note is emitted on stderr (or merged into output by CliRunner).
    assert "ignoring --instance" in combined(result)


def test_call_a2a_reads_message_from_stdin(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """When MESSAGE is omitted and stdin is piped, the message is read from stdin."""
    captured: dict[str, object] = {}

    def _fake_call_a2a(*, app_host: str, profile: str | None, message: str) -> int:
        captured["message"] = message
        return 0

    monkeypatch.setattr("agent_foundry.cli.call.call_a2a", _fake_call_a2a)

    result = runner.invoke(
        app,
        ["call", "a2a", "--app-host", "demo.databricks.app"],
        input="message from stdin\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["message"] == "message from stdin"


def test_call_rejects_instance_not_declared_in_repo(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An instance that foundry.toml cannot resolve is a usage error pointing at --app-host."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agent_foundry.cli.call.call_mcp",
        lambda *, app_host, profile, message: 0,
    )

    result = runner.invoke(app, ["call", "mcp", "--instance", "definitely-not-an-instance", "hi"])

    assert result.exit_code == 2, combined(result)
    assert "--app-host" in combined(result)


# ---------------------------------------------------------------------------
# genie import -- with mocked WorkspaceClient
# ---------------------------------------------------------------------------


def test_genie_import_dispatches_to_export_main(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_export_main(argv: list[str] | None = None) -> int:
        captured["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr("bricks.genie.scripts.export.main", _fake_export_main)

    result = runner.invoke(
        app,
        [
            "genie",
            "import",
            "--space-id",
            "abc-123",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "--space-id" in captured["argv"] and "abc-123" in captured["argv"]
    assert "--output" in captured["argv"]


def test_genie_import_requires_space_id(runner: CliRunner) -> None:
    result = runner.invoke(app, ["genie", "import"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# destroy -- with mocked orchestrator so no real bricks fire
# ---------------------------------------------------------------------------


def test_destroy_calls_orchestrator(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run_destroy(**kwargs: object) -> int:
        assert kwargs.get("instance") == "demo"
        assert kwargs.get("dry_run") is True
        return 0

    monkeypatch.setattr("agent_foundry.deploy.cli.destroy._run_destroy", _fake_run_destroy)

    result = runner.invoke(app, ["destroy", "--instance", "demo", "--dry-run"])
    assert result.exit_code == 0, combined(result)


def test_destroy_summary_shows_skip_reason(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_foundry.bricks import BrickResult, DestroyPlan
    from agent_foundry.deploy.cli.destroy import _summarise_destroy
    from agent_foundry.public import DestroyResult

    result = DestroyResult(
        instance_name="demo",
        target="dev",
        bricks={
            "Jobs": [BrickResult(name="job", status="skipped", details={"reason": "nothing to destroy"})],
            "Genie Spaces": [BrickResult(name="space", status="skipped")],
            "SQL Warehouse": [BrickResult(name="demo", status="ok")],
        },
        plan=DestroyPlan(items=[]),
        forced=True,
    )

    assert _summarise_destroy(result) == 0
    out = capsys.readouterr().out
    assert "[SKIP] Jobs/job (nothing to destroy)\n" in out
    assert "[SKIP] Genie Spaces/space\n" in out
    assert "[OK] SQL Warehouse/demo\n" in out


def test_destroy_offline_requires_dry_run(runner: CliRunner) -> None:
    result = runner.invoke(app, ["destroy", "--instance", "demo", "--offline"])
    assert result.exit_code == 1, combined(result)
    assert "--offline requires --dry-run" in combined(result)


def test_destroy_passes_offline_with_dry_run(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_destroy(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("agent_foundry.deploy.cli.destroy._run_destroy", _fake_run_destroy)
    result = runner.invoke(app, ["destroy", "--instance", "demo", "--dry-run", "--offline"])
    assert result.exit_code == 0, combined(result)
    assert captured["offline"] is True
    assert captured["dry_run"] is True


def test_destroy_unknown_brick_returns_2(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        [
            "destroy",
            "--instance",
            "demo",
            "--brick",
            "nope",
        ],
    )
    assert result.exit_code == 2
    assert "Unknown brick" in combined(result)
