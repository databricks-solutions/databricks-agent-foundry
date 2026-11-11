"""Genie space create: the parent folder must exist before the API call."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from bricks.genie.scripts.deploy import create_space


def test_create_space_makes_parent_folder_before_posting() -> None:
    client = MagicMock()
    client.api_client.do.return_value = {"space_id": "s1"}
    payload = {"title": "t", "parent_path": "/Workspace/Shared/agent-foundry/dev/app"}

    assert create_space(client, payload) == {"space_id": "s1"}

    assert client.mock_calls[:2] == [
        call.workspace.mkdirs("/Shared/agent-foundry/dev/app"),
        call.api_client.do("POST", "/api/2.0/genie/spaces", body=payload),
    ]


def test_create_space_without_parent_path_makes_no_folder() -> None:
    client = MagicMock()
    client.api_client.do.return_value = {"space_id": "s1"}

    create_space(client, {"title": "t"})

    client.workspace.mkdirs.assert_not_called()
