"""
Programmatic Agent Callers (A2A and MCP)
=========================================
Helpers to invoke a deployed Agent Bricks app via the Model Context
Protocol (MCP) or the Agent-to-Agent (A2A) protocol.

Public surface:

* :func:`call_a2a` -- streaming A2A call against an already-resolved host.
* :func:`call_mcp` -- single-turn MCP ``ask_agent`` against an already-resolved host.
* :func:`resolve_app_host` -- friendly resolver that turns
  ``(--instance, --target)`` into ``(app_host, profile)`` via
  ``WorkspaceClient.apps.get(...)``. The CLI uses it so users do not
  have to know the random Databricks Apps hostname.

Promoted from ``scripts/call_agent_{a2a,mcp}.py`` to the package proper
as part of issue #96 so downstream consumers can drive the agent from
their own code or via ``agent-foundry call {a2a,mcp}``.

Runtime dependencies
--------------------
The protocol-specific imports (``a2a-sdk``, ``databricks-mcp``,
``httpx``) and the Databricks SDK lookup in :func:`resolve_app_host`
are guarded inside each function so importing this module does not
require the optional ``[call]`` extra.  Missing extras yield a friendly
``ImportError`` pointing at the install command.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from urllib.parse import urlparse

from agent_foundry.loader import FoundryConfigError, load_instance_config


def _missing_extra(extra: str, exc: Exception) -> ImportError:
    return ImportError(
        f"agent-foundry's '{extra}' extra is required for this command. "
        f"Install with: uv pip install 'agent-foundry[call]'  "
        f"(original error: {exc})"
    )


def resolve_app_host(
    *,
    instance: str | None,
    target: str = "dev",
    profile: str | None = None,
    app_host: str | None = None,
    project_root: Path | None = None,
) -> tuple[str, str | None]:
    """Resolve ``(app_host, profile)`` for :func:`call_a2a` / :func:`call_mcp`.

    ``--app-host`` (when given) wins as an explicit override and is
    returned as-is alongside the caller-supplied ``profile``. Otherwise
    we load the instance's :class:`~agent_foundry.config.InstanceConfig`
    and look up the deployed app's hostname via
    ``WorkspaceClient.apps.get(<app_resource_name>).url``.

    The CLI ``--profile`` overrides the target's configured profile when
    both are supplied; otherwise we fall back to the ``foundry.toml``
    target's ``profile``, and finally to the SDK's default credential
    resolution.

    Raises :class:`ValueError` when neither ``app_host`` nor ``instance``
    is supplied, or when the instance cannot be loaded from
    ``foundry.toml`` (caller is expected to translate this into a
    user-friendly CLI error).
    """
    if app_host:
        return app_host, profile

    if not instance:
        raise ValueError("must specify --instance (with optional --target) or --app-host")

    try:
        from agent_foundry.workspace_client import get_workspace_client
    except ImportError as exc:  # pragma: no cover - import error path
        raise _missing_extra("call", exc) from exc

    root = project_root or Path.cwd().resolve()
    try:
        cfg = load_instance_config(root, instance=instance, target_name=target)
    except FoundryConfigError as exc:
        raise ValueError(f"{exc} Pass --app-host to call an App that is not declared in this repo.") from exc
    resolved_profile = profile or cfg.target.profile or None

    ws = get_workspace_client(profile=resolved_profile)
    app = ws.apps.get(cfg.app_resource_name)

    parsed = urlparse(app.url or "")
    if not parsed.netloc:
        raise ValueError(
            f"could not parse hostname from app URL: {app.url!r} (app_resource_name={cfg.app_resource_name!r})"
        )
    return parsed.netloc, resolved_profile


def call_mcp(
    *,
    app_host: str,
    profile: str | None,
    message: str,
) -> int:
    """Call the deployed agent via MCP (single-turn ``ask_agent`` tool).

    Returns the process exit code (0 on success).
    """
    try:
        from databricks_mcp import DatabricksMCPClient

        from agent_foundry.workspace_client import get_workspace_client
    except ImportError as exc:  # pragma: no cover - import error path
        raise _missing_extra("call", exc) from exc

    ws = get_workspace_client(profile=profile)
    server_url = f"https://{app_host}/api/mcp/"

    print(f"Connecting to MCP server: {server_url}")
    client = DatabricksMCPClient(server_url=server_url, workspace_client=ws)

    print("Listing tools...")
    tools = client.list_tools()
    for tool in tools:
        print(f"  - {tool.name}: {tool.description}")

    print(f"\nCalling ask_agent with: {message!r}")
    result = client.call_tool("ask_agent", {"question": message})

    print("\n--- Agent Response ---")
    print(result)
    return 0


def call_a2a(
    *,
    app_host: str,
    profile: str | None,
    message: str,
) -> int:
    """Call the deployed agent via the A2A streaming protocol.

    Returns the process exit code (0 on success).
    """
    try:
        return asyncio.run(_call_a2a_async(app_host=app_host, profile=profile, message=message))
    except ImportError as exc:  # pragma: no cover - import error path
        raise _missing_extra("call", exc) from exc


async def _call_a2a_async(
    *,
    app_host: str,
    profile: str | None,
    message: str,
) -> int:
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types.a2a_pb2 import (
        Message,
        Part,
        Role,
        SendMessageConfiguration,
        SendMessageRequest,
        TaskState,
    )

    from agent_foundry.workspace_client import get_workspace_client

    ws = get_workspace_client(profile=profile)
    headers: dict[str, str] = ws.config.authenticate()

    base_url = f"https://{app_host}"
    card_path = "/api/.well-known/agent-card.json"

    print(f"Discovering agent at {base_url}{card_path}")

    async with httpx.AsyncClient(headers=headers, timeout=300) as httpx_client:
        resolver = A2ACardResolver(httpx_client, base_url, agent_card_path=card_path)
        agent_card = await resolver.get_agent_card()

        print(f"Agent: {agent_card.name}")
        print(f"Description: {agent_card.description}")
        for skill in agent_card.skills or []:
            print(f"  Skill: {skill.name} -- {skill.description}")

        # Agent card may carry relative URLs (e.g. "/api/a2a"); resolve
        # against the base URL so the transport has absolute URLs.
        for iface in agent_card.supported_interfaces:
            if iface.url and not iface.url.startswith("http"):
                iface.url = f"{base_url}{iface.url}"
        if getattr(agent_card, "url", None) and not agent_card.url.startswith("http"):
            agent_card.url = f"{base_url}{agent_card.url}"

        config = ClientConfig(streaming=True, httpx_client=httpx_client)
        factory = ClientFactory(config)
        client = factory.create(card=agent_card)

        request = SendMessageRequest(
            message=Message(
                message_id=str(uuid.uuid4()),
                role=Role.ROLE_USER,
                parts=[Part(text=message)],
            ),
            configuration=SendMessageConfiguration(),
        )

        print(f"\nSending message: {message!r}\n")

        async for event in client.send_message(request):
            if event.HasField("task"):
                task = event.task
                print(f"[task] id={task.id} state={task.status.state}")
                for artifact in task.artifacts:
                    for part in artifact.parts:
                        if part.text:
                            print(part.text, end="", flush=True)
            elif event.HasField("artifact_update"):
                for part in event.artifact_update.artifact.parts:
                    if part.text:
                        print(part.text, end="", flush=True)
            elif event.HasField("status_update"):
                state = event.status_update.status.state
                if state == TaskState.TASK_STATE_COMPLETED:
                    print("\n\n[completed]")
                elif state == TaskState.TASK_STATE_FAILED:
                    msg = event.status_update.status.message
                    err = ""
                    if msg:
                        err = " ".join(p.text for p in msg.parts if p.text)
                    print(f"\n\n[failed] {err}")
                else:
                    print(f"[status] state={state}")
            elif event.HasField("message"):
                for part in event.message.parts:
                    print(part.text if part.text else part)
            else:
                print(f"[event] {event}")

    return 0
