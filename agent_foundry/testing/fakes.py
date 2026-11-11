"""Shared in-memory ``WorkspaceClient`` for payload deploy unit tests.

Covers the surfaces MCP and rest_mcp need: workspace file I/O for
``agent_foundry.state`` round-trips, REST recording for UC connections /
MCP services, Apps URL + SP lookup, and SP secret mint/revoke.
"""

from __future__ import annotations

from typing import Any

from databricks.sdk.errors import NotFound

_SERVICES_API = "/api/2.1/unity-catalog/mcp-services"
_SCHEMAS_API = "/api/2.1/unity-catalog/schemas"
_PERMISSIONS_PREFIX = "/api/2.1/unity-catalog/permissions/mcp_service/"


class _FakeApiClient:
    """Minimal ``ws.api_client`` recording connection + MCP-service CRUD calls.

    Routes by path so the same fake backs both layers: the ``connections`` API
    (keyed by body ``name``) and the Beta ``mcp-services`` / ``schemas`` /
    ``permissions`` APIs (services keyed by their 3-level full name, derived from
    the create ``parent`` + ``mcp_service_id`` query params).
    """

    def __init__(
        self,
        existing: dict[str, dict[str, Any]] | None = None,
        *,
        services: dict[str, dict[str, Any]] | None = None,
        schemas: set[str] | None = None,
        service_preview_off: bool = False,
    ) -> None:
        self.existing: dict[str, dict[str, Any]] = dict(existing or {})
        self.services: dict[str, dict[str, Any]] = dict(services or {})
        self.schemas: set[str] = set(schemas or set())
        self.grants: list[tuple[str, dict[str, Any]]] = []
        self.service_preview_off = service_preview_off
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def do(
        self,
        method: str,
        path: str,
        *,
        query: Any = None,
        headers: Any = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del headers
        self.calls.append((method, path, body))
        if path.startswith(_PERMISSIONS_PREFIX):
            return self._do_permissions(path, body)
        if path == _SCHEMAS_API or path.startswith(_SCHEMAS_API + "/"):
            return self._do_schemas(method, path, body)
        if path == _SERVICES_API or path.startswith(_SERVICES_API + "/"):
            return self._do_services(method, path, query, body)
        return self._do_connections(method, path, body)

    def _do_connections(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        name = path.rsplit("/", 1)[-1]
        if method == "GET":
            if name in self.existing:
                return self.existing[name]
            raise NotFound(f"Connection '{name}' does not exist.")
        if method == "POST":
            assert body is not None
            self.existing[body["name"]] = {"name": body["name"], "options": body["options"]}
            return self.existing[body["name"]]
        if method == "PATCH":
            assert body is not None
            current = self.existing.setdefault(name, {"name": name, "options": {}})
            if "options" in body:
                current["options"] = dict(body["options"])
            return current
        if method == "DELETE":
            self.existing.pop(name, None)
        return {}

    def _do_schemas(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if method == "GET":
            full = path.rsplit("/", 1)[-1]
            if full in self.schemas:
                return {"full_name": full}
            raise NotFound(f"Schema '{full}' does not exist.")
        if method == "POST":
            assert body is not None
            self.schemas.add(f"{body['catalog_name']}.{body['name']}")
        return {}

    def _do_services(self, method: str, path: str, query: Any, body: dict[str, Any] | None) -> dict[str, Any]:
        if method == "POST":  # create: full name comes from query params
            if self.service_preview_off:
                raise RuntimeError("MCP Services preview is not enabled for this account.")
            parent = str((query or {}).get("parent", ""))  # "schemas/{cat}.{schema}"
            leaf = str((query or {}).get("mcp_service_id", ""))
            full = f"{parent.split('/', 1)[-1]}.{leaf}"
            assert body is not None
            self.services[full] = {"name": f"mcp-services/{full}", "config": dict(body["config"])}
            return self.services[full]
        full = path[len(_SERVICES_API) + 1 :]
        if method == "GET":
            if full in self.services:
                return self.services[full]
            raise NotFound(f"MCP Service '{full}' does not exist.")
        if method == "PATCH":  # merge the single config.<field> into stored config
            assert body is not None
            self.services.setdefault(full, {"name": f"mcp-services/{full}", "config": {}})
            self.services[full]["config"].update(body["config"])
            return self.services[full]
        if method == "DELETE":
            self.services.pop(full, None)
        return {}

    def _do_permissions(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        full = path[len(_PERMISSIONS_PREFIX) :]
        self.grants.append((full, dict(body or {})))
        return {}


class _FakeWorkspaceFs:
    """In-memory stand-in for the ``ws.workspace`` file API used by state.py."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def export(self, path: str) -> Any:
        if path not in self.files:
            raise FileNotFoundError(path)
        return type("_Resp", (), {"content": self.files[path]})()

    def mkdirs(self, path: str) -> None:
        del path

    def upload(self, *, path: str, content: Any, format: Any, overwrite: bool) -> None:  # noqa: A002
        del format, overwrite
        self.files[path] = content.read()

    def delete(self, path: str) -> None:
        self.files.pop(path, None)


class _FakeApp:
    def __init__(self, url: str, sp_client_id: str, sp_id: str) -> None:
        self.url = url
        self.service_principal_client_id = sp_client_id
        self.service_principal_id = sp_id


class _FakeApps:
    def __init__(self, url: str, sp_client_id: str, sp_id: str) -> None:
        self._app = _FakeApp(url, sp_client_id, sp_id)

    def get(self, name: str) -> _FakeApp:
        del name
        return self._app


class _FakeConfig:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeSecretResp:
    def __init__(self, secret: str, secret_id: str) -> None:
        self.secret = secret
        self.id = secret_id


class _FakeSpSecretsProxy:
    """Records minted/revoked App SP OAuth secrets (``ws.service_principal_secrets_proxy``)."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self._n = 0

    def create(self, *, service_principal_id: str, lifetime: str | None = None) -> _FakeSecretResp:
        del lifetime
        self._n += 1
        self.created.append(service_principal_id)
        return _FakeSecretResp(secret=f"sp-secret-{self._n}", secret_id=f"secret-id-{self._n}")

    def delete(self, *, service_principal_id: str, secret_id: str) -> None:
        self.deleted.append((service_principal_id, secret_id))


class FakeUcLookup:
    """Unity Catalog getter (``ws.functions`` / ``ws.schemas``) that knows only *existing* names."""

    def __init__(self, *existing: str) -> None:
        self.existing = set(existing)

    def get(self, full_name: str) -> None:
        if full_name not in self.existing:
            raise NotFound(f"{full_name} does not exist")


class FakeWorkspaceClient:
    """Duck-typed ``WorkspaceClient`` for MCP and rest_mcp unit tests.

    ``ws.workspace`` matches production state I/O. ``_files`` aliases the
    nested store so MCP tests that inspect ``fake_ws._files`` keep working.
    """

    def __init__(
        self,
        *,
        existing: dict[str, dict[str, Any]] | None = None,
        services: dict[str, dict[str, Any]] | None = None,
        schemas: set[str] | None = None,
        service_preview_off: bool = False,
        app_url: str = "https://app-x-123.aws.databricksapps.com",
        sp_client_id: str = "sp-client-1",
        sp_id: str = "111222333",
        host: str = "https://ws.cloud.databricks.com",
    ) -> None:
        self.api_client = _FakeApiClient(
            existing,
            services=services,
            schemas=schemas,
            service_preview_off=service_preview_off,
        )
        self.workspace = _FakeWorkspaceFs()
        self.apps = _FakeApps(app_url, sp_client_id, sp_id)
        self.config = _FakeConfig(host)
        self.service_principal_secrets_proxy = _FakeSpSecretsProxy()

    @property
    def _files(self) -> dict[str, bytes]:
        return self.workspace.files

    def posts(self) -> list[dict[str, Any]]:
        return [body for method, _path, body in self.api_client.calls if method == "POST" and body is not None]

    def connection_patches(self) -> list[dict[str, Any]]:
        return [
            body
            for method, path, body in self.api_client.calls
            if method == "PATCH" and body is not None and _SERVICES_API not in path
        ]

    def deletes(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for method, path, _body in self.api_client.calls if method == "DELETE"]

    def service_patches(self) -> list[tuple[str, dict[str, Any] | None]]:
        """``(path, body)`` for every PATCH against the mcp-services API."""
        return [
            (path, body) for method, path, body in self.api_client.calls if method == "PATCH" and _SERVICES_API in path
        ]
