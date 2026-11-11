"""Regression tests for the agent payload's chat-history toggle reader.

Guards the #338 contract: the toggle lives in ``spec.ui.chat_history``
and is surfaced through ``ctx.variables['chat_history_enabled']`` (the
deploy variable bag).  A prior implementation dereferenced
``ctx.config.chat_history`` -- but ``ctx.config`` is the framework
``InstanceConfig``, which no longer carries that attribute, so every
agent deploy raised ``AttributeError`` at the Lakebase provision step.
"""

from __future__ import annotations

from pathlib import Path

from agent_foundry.bricks import DeployContext
from payloads.agent.deploy.chat_history import chat_history_enabled


def _ctx(
    tmp_path: Path,
    *,
    variables: dict[str, str] | None = None,
    config: object | None = None,
) -> DeployContext:
    return DeployContext(
        instance_dir=tmp_path,
        target="dev",
        variables=variables or {},
        config=config,
    )


class TestChatHistoryEnabled:
    def test_true_from_variable_bag(self, tmp_path: Path) -> None:
        assert chat_history_enabled(_ctx(tmp_path, variables={"chat_history_enabled": "true"})) is True

    def test_false_from_variable_bag(self, tmp_path: Path) -> None:
        assert chat_history_enabled(_ctx(tmp_path, variables={"chat_history_enabled": "false"})) is False

    def test_defaults_true_when_absent(self, tmp_path: Path) -> None:
        assert chat_history_enabled(_ctx(tmp_path)) is True

    def test_ignores_config_without_chat_history_attr(self, tmp_path: Path) -> None:
        """A non-None ``config`` lacking ``chat_history`` (the framework
        ``InstanceConfig`` post-#338) must not raise -- the toggle is read
        from the variable bag, never off ``ctx.config``."""

        class _InstanceConfigLike:
            """Stand-in for InstanceConfig: no ``chat_history`` attribute."""

        ctx = _ctx(tmp_path, variables={"chat_history_enabled": "false"}, config=_InstanceConfigLike())
        assert chat_history_enabled(ctx) is False
