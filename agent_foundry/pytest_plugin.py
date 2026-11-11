"""Pytest plugin: skip gates for live / component tests.

Registered via ``[project.entry-points.pytest11]`` so every repo that
installs ``agent-foundry`` (including ``agent-foundry init`` consumers)
gets the flags, markers and collection skip without copying a root
``conftest.py`` (#434).

Every test under a ``tests/live/`` folder is marked ``live``: it checks a
CLI deployment in a real workspace and only runs with ``--run-live``.
``gone`` tests there run after ``destroy`` (``--run-gone``). Everything
else is offline unless marked ``component``.

Also opts pytest out of Labs telemetry and sets the skip-env tripwire so
a missed factory call cannot hang CI on credential discovery.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, NoReturn
from unittest.mock import patch

import pytest

from agent_foundry import env
from agent_foundry.testing.live import is_live_path
from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

if TYPE_CHECKING:
    from agent_foundry.testing.live import LiveTarget

LIVE_MARKER = "live"
_MARKERS = (
    f"{LIVE_MARKER}: checks a CLI deployment in a real workspace; every test under tests/live/ (use --run-live)",
    "component: packaging / packaged-CLI smokes (wheel + fresh venv; use --run-component)",
    "gone: after destroy, checks the deployment left nothing behind (use --run-gone with --run-live)",
)
# (marker, flag that enables it)
_GATES = (
    (LIVE_MARKER, "--run-live"),
    ("component", "--run-component"),
    ("gone", "--run-gone"),
)
_PUBLISHED_CACHE_KEY = "agent_foundry/live_variables"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_UV_NETWORK_MARKERS = (
    "network was disabled",
    "Connection refused",
    "tunnel error",
    "Failed to fetch",
    "dns error",
    "no address associated",
    # Private pins live on PyPI; public PyPI
    # reports this as an unsatisfiable solve rather than a fetch error.
    "No solution found when resolving",
    "there is no version of",
)


def strip_ansi(text: str) -> str:
    """Strip ANSI so ``'--flag' in output`` works when Rich colors the dashes."""
    return _ANSI_RE.sub("", text)


def uv_pip_install_cmd(python: str, *specs: str) -> list[str]:
    """``uv pip install`` into a temp venv, using CI's PyPI index when set."""
    cmd = ["uv", "pip", "install", "--python", python, *specs]
    index = os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL")
    if index:
        cmd.extend(["--default-index", index])
    return cmd


def require_uv_install(install: subprocess.CompletedProcess[str], what: str) -> None:
    """Handle ``uv pip install`` used by ``@pytest.mark.component`` smokes.

    Unreachable indexes skip locally so a laptop without PyPI can still
    run the unit job. GitHub Actions sets ``CI``; a skip there is a fail.
    """
    if install.returncode == 0:
        return
    combined = install.stdout + install.stderr
    if any(marker in combined for marker in _UV_NETWORK_MARKERS):
        msg = f"uv pip install needs pypi/PyPI access ({what}):\n{combined}"
        if os.environ.get("CI"):
            pytest.fail(msg)
        pytest.skip(msg)
    pytest.fail(f"{what} failed (rc={install.returncode}):\n{combined}")


def pytest_configure(config: pytest.Config) -> None:
    """``config`` must stay without a default: pluggy drops defaulted hook arguments."""
    os.environ.setdefault(env.AGENT_FOUNDRY_TELEMETRY_DISABLED.name, "true")
    os.environ["NO_COLOR"] = "1"
    os.environ.pop("FORCE_COLOR", None)
    for line in _MARKERS:
        config.addinivalue_line("markers", line)
    if not _live_workspace_flags(config):
        # Inherited by CLI subprocesses (plugin patches are not).
        os.environ.setdefault(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    else:
        os.environ.pop(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, None)


def _live_workspace_flags(config: pytest.Config) -> bool:
    """True when ``--run-live`` (the flag that needs a real WorkspaceClient) was passed.

    Do not pass ``default=`` into ``getoption``: if this runs before
    ``pytest_addoption``, a default of False hides the missing option and
    leaves the skip env set while ``--run-live`` still unskips tests.
    """
    try:
        return bool(config.getoption("--run-live"))
    except (ValueError, AttributeError):
        return False


def pytest_sessionstart(session: pytest.Session) -> None:
    """Options are always registered here; clear skip for live runs."""
    if _live_workspace_flags(session.config):
        os.environ.pop(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, None)


def _blocked_workspace_client(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError(
        "databricks.sdk.WorkspaceClient is blocked in unit tests; "
        "mock the client or pass --run-live "
        "(component tests use a subprocess + --offline, not this patch)"
    )


@pytest.fixture(scope="session", autouse=True)
def _block_default_workspace_client(pytestconfig: pytest.Config) -> Iterator[None]:
    """Fail fast instead of hanging on SDK credential discovery.

    ``_try_get_workspace_client`` swallows errors and returns None, but
    ``WorkspaceClient()`` can stall for minutes on EMU runners before
    that exception. Live runs keep the real client.
    """
    if _live_workspace_flags(pytestconfig):
        yield
        return
    with patch("databricks.sdk.WorkspaceClient", _blocked_workspace_client):
        yield


@pytest.fixture(scope="session")
def live_target(pytestconfig: pytest.Config) -> LiveTarget:
    """The CLI-deployed instance a ``tests/live/`` check verifies.

    Skips without ``--run-live``. A missing binding or instance skips
    locally and fails when ``AGENT_FOUNDRY_REQUIRE_LIVE=1`` (canary jobs).
    """
    from agent_foundry.testing.live import load_live_target
    from agent_foundry.testing.workspace import WorkspaceConfigError

    if not pytestconfig.getoption("--run-live"):
        pytest.skip("needs --run-live flag")
    try:
        return load_live_target()
    except WorkspaceConfigError as exc:
        _missing_live(f"live binding missing: {exc}")


def _missing_live(reason: str) -> NoReturn:
    from agent_foundry.testing.live import live_required

    if live_required():
        pytest.fail(reason)
    pytest.skip(reason)


def _published_cache_key(target: LiveTarget) -> str:
    return f"{_PUBLISHED_CACHE_KEY}/{target.instance}"


@pytest.fixture(scope="session")
def live_ws(live_target: LiveTarget) -> Any:
    """WorkspaceClient for the live binding (host-checked against the SDK)."""
    from agent_foundry.testing.workspace import client_for_binding

    return client_for_binding(live_target.binding)


@pytest.fixture(scope="session")
def live_variables(pytestconfig: pytest.Config, live_target: LiveTarget, live_ws: Any) -> dict[str, str]:
    """Published brick outputs of the deployed instance (``warehouse_id``, ...).

    Also recorded in the pytest cache, because ``destroy`` deletes the
    state they come from and the ``gone`` checks need them afterwards.
    """
    from agent_foundry.testing.live import load_live_variables

    variables = load_live_variables(live_target, live_ws)
    if pytestconfig.cache is not None:
        pytestconfig.cache.set(_published_cache_key(live_target), variables)
    return variables


@pytest.fixture(scope="session")
def gone_variables(pytestconfig: pytest.Config, live_target: LiveTarget) -> dict[str, str]:
    """What ``live_variables`` recorded while the deployment was up, for ``gone`` checks."""
    cached = pytestconfig.cache.get(_published_cache_key(live_target), None) if pytestconfig.cache else None
    if not isinstance(cached, dict) or not cached:
        _missing_live("no published outputs recorded; run the live checks before destroy in the same checkout")
    return {str(key): str(value) for key, value in cached.items()}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run tests/live checks against a CLI deployment in a real Databricks workspace",
    )
    parser.addoption(
        "--run-component",
        action="store_true",
        default=False,
        help="Run packaging / packaged-CLI smokes (wheel + fresh venv; no Databricks)",
    )
    parser.addoption(
        "--run-gone",
        action="store_true",
        default=False,
        help="Run gone checks (after destroy; with --run-live) that the deployment left nothing behind",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark ``tests/live/`` items, then skip every gated marker whose flag is off.

    ``tryfirst`` so the folder marker exists before ``-m live`` deselection runs.
    """
    disabled = [(marker, flag) for marker, flag in _GATES if not config.getoption(flag)]
    for item in items:
        if is_live_path(item.path):
            item.add_marker(LIVE_MARKER)
        for marker, flag in disabled:
            if item.get_closest_marker(marker) is not None:
                item.add_marker(pytest.mark.skip(reason=f"needs {flag} flag"))
