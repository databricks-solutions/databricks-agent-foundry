# MCP Proxy payload (`rest_mcp`) — design

> A design for the **MCP Proxy**: a Databricks App payload that wraps one or
> more arbitrary REST APIs as a **governed MCP server**. It is the config-driven
> REST→MCP translation layer the platform does not ship, attached the native
> Databricks way so it inherits AI Gateway / MCP Catalog governance.
>
> The MCP-hosting app is not new infrastructure: it is a *payload*, selected by
> `adapter: rest_mcp` in `payload.yml` and deployed by the same
> [`deploy_on_host()`](../../agent_foundry/deploy/app_host.py) sequence the
> agent uses. The durable value is the **REST→MCP translation layer**; the
> attachment rides the native governed path, so any MCP client (Playground,
> Supervisor API, Cursor) reuses it **with no agent-runtime changes.** For the
> seam this builds on see [Author a payload](../how-to/author-a-payload.md); for
> the App lifecycle see [Architecture](architecture.md).

## Platform validation (checked Jul 2026)

Confirmed against the live Databricks docs and the template repo:

- **The template is alive and first-class.** `databricks/app-templates`
  → `mcp-server-hello-world` is now a built-in App template ("MCP Server – Hello
  World", Agents category). Shape: `server/{app,main,tools,utils}.py`, `app.yaml`
  = `command: ["uv","run","custom-mcp-server"]`, uvicorn ASGI entrypoint, and
  `FastMCP(...).http_app(stateless_http=True)`. The template's own comment
  documents *why* stateless: the Databricks Assistant does not send
  `mcp-session-id`, and Apps scale horizontally. **We model the payload runtime
  on it verbatim.**
- **Hosting a proxy App is still the recommended custom-MCP path** ("Host your own
  MCP server as a Databricks app"), served at `https://<app-url>/mcp` over
  Streamable HTTP.
- **The governance model graduated.** The bare `is_mcp` HTTP connection is
  now a first-class **MCP Service (Beta)** — a Unity Catalog securable
  `catalog.schema.mcp_service` invoked through **Unity AI Gateway**, with tool
  selection (prefix / exact patterns), service policies (allow / deny / approve
  per call), rate limits, and audit / usage / trace system tables.
- **Token handling is fully managed.** Databricks stores the credentials and
  handles OAuth flows + token refresh; the agent never sees tokens. Hop-1 auth
  is shared-principal (bearer / OAuth M2M / U2M-shared) or per-user OAuth U2M.
- **The consumption URL changed because the platform evolved.** The framework's
  `type: external` + `{host}/api/2.0/mcp/external/{connection_name}` is the
  *first-generation* external-MCP path: a server behind a UC HTTP connection,
  reached through a managed proxy and addressed by **connection name**.
  Databricks has since promoted external MCP into a first-class Unity Catalog
  securable — the **MCP Service**, addressed by its **three-level name** and
  invoked through Unity AI Gateway. The current, governance-forward URL is
  therefore **`{host}/ai-gateway/mcp-services/{catalog}.{schema}.{service}`**
  (custom-app direct is still `{app_url}/mcp`). This design targets that URL as
  the canonical governed path; `/api/2.0/mcp/external/…` is treated as
  superseded. Closing this framework-lag is the one framework-contract change
  this work implies — see [Attachment strategies](#attachment-two-strategies).
- **Caveats to respect:** MCP Services are **Beta** (need Unity AI Gateway Beta +
  Managed MCP Servers preview at the account); creation is UI / REST only (**no
  SQL DDL**); and the docs note *registering Apps as an MCP Service is not
  currently supported* — we register the app as a **generic external HTTP MCP
  server** (HTTP connection → `{app_url}/mcp`), not via a native App picker, so
  reachability + hop-1 auth into the OAuth-gated app URL are the main risks
  (see [Operational notes](#operational-notes)).
- **New overlap to scope around.** Databricks now ships built-in MCP Services for
  common SaaS (Slack, GitHub, Atlassian, Google Drive / Calendar / Gmail,
  SharePoint) in `system.ai`. The payload's durable value therefore narrows to
  **bespoke / internal REST APIs** with no built-in service — which is exactly
  its purpose.

## Operational notes

- **MCP Services are early Beta.** Enablement is an **account-level** preview
  (Account console → Previews → "AI Gateway (Beta)"), not per-workspace, and
  services are **external-type only** today. Expect Beta churn in field names;
  only the bare `/ai-gateway/mcp-services/<fqn>` route is served
  (`/api/2.0/ai-gateway/...` is not).
- **App reachability is the main operational risk.** The managed proxy /
  serverless egress cannot reach an app-hosted MCP endpoint until the **full
  MCP FQDN is added to the serverless outbound allowlist**. Private link to the
  app is fragile.
- **The UC connection calls the server as plain REST.** Its `base_path` / host
  must point at the **MCP mount** (`{app_url}/mcp/<api>`), not the REST root,
  or `initialize` is forwarded to the wrong path and returns 307/404.
- **Create connections and services via REST.** Set
  `is_mcp_connection = "true"` explicitly; the Managed-OAuth UI wizard can land
  connections with `is_mcp=False`. Create the service via
  `POST /api/2.1/unity-catalog/mcp-services` (`api_client.do`; the SDK lags).

## Three-layer mapping

```
Layer 1 — agent_foundry/  (contracts only — users never touch)
  ├─ AppPayloadAdapter protocol             ✅ exists
  ├─ AppPayloadSpec / AppInfraSpec          ✅ exists
  ├─ deploy_on_host / destroy_on_host       ✅ exists (generic App lifecycle)
  ├─ publish_app_state (App URL)            ✅ exists
  ├─ PayloadFile / PayloadConfig envelope   ✅ exists (adapter + opaque spec)
  └─ entry-point registry + scaffold compose ✅ exists

Layer 2 — payloads/rest_mcp/  (power users extend, bound by Layer-1 contracts)
  ├─ adapter.py     RestMcpPayloadAdapter(AppPayloadAdapter)
  ├─ contracts.py   the payload-owned typed spec   ← the "config service"
  ├─ runtime/       FastMCP + FastAPI server + pure REST→MCP engine
  ├─ deploy/        UC connection + MCP Service provision() step + state + scaffolds
  └─ schemas/       payload-owned JSON schema (mirrors payloads/agent/schemas)

Layer 3 — instances/<name>/  (config only — scaffolded, in the portfolio system)
  ├─ payloads/rest_mcp/payload.yml       adapter: rest_mcp + spec (APIs + tools)
  └─ mcp_servers/<api>/server.yml        one per API (the agent consumes it)
```

`payloads/rest_mcp/` mirrors `payloads/agent/` one-for-one. That parallelism is
the point: a power user who understands the agent payload already understands
this one.

### The one net-new capability: register the MCP surface in UC

The only thing Layer 1 does not already provide is registering the hosted MCP
surface in Unity Catalog — creating/updating the **HTTP connection** (host =
`{app_url}/{mount}`) and, when governance is on, the **MCP Service** securable
built from it (UI/REST only — no SQL DDL). By design this lives
**inside the payload** (`payloads/rest_mcp/deploy/connection.py`), driven by the
adapter's `provision()` hook, publishing the connection / service name into the
variable bag via `load_state_outputs()`. No framework change. Promote it to a
framework helper (next to `publish_app_state`) only if a second payload ever
needs it (YAGNI).

## The configuration service (payload-owned `spec`)

The "config service" is the payload-owned typed `spec` — the analog of
`AgentPayloadSpec`. Its guiding principle, and the answer to *"how do we keep the
mapping consistent and generic":*

> **One tool declaration is the single source of truth for three derived
> artifacts that must never drift:** (a) the MCP tool `inputSchema` advertised on
> the MCP card, (b) the `httpx` request the engine builds, and (c) the response
> projection. All three are *generated* from the same `args` list, so the card can
> never lie about what the call actually does.

By design, **one hosting app fronts multiple REST APIs and maps
each to its own MCP server**: one app → N FastMCP mounts (one per API) → N UC
connections / MCP Services → N REST APIs. Mapping each REST API to its own MCP
surface (rather than one shared endpoint) is the better fit for the MCP-Service
model, because each service is governed independently (its own tool selection,
service policy, grants, and rate limit). One app to deploy and pay for; N
independently governed MCP surfaces.

```yaml
# instances/<name>/payloads/rest_mcp/payload.yml
payload:
  adapter: rest_mcp
  spec:
    apis:                                   # each entry -> its own MCP mount + connection/service
      - name: orders                        # mount /mcp/orders; MCP Service <cat>.<schema>.orders
        rest:                               # hop 2: app -> upstream REST API
          base_url: "https://orders.internal.example.com"
          allowed_hosts: ["orders.internal.example.com"]   # egress allow-list, https-only
          auth:
            mode: static                    # discriminated union: none | static | obo
            secret_scope: orders_api
            secret_key: bearer_token
          defaults:
            timeout_s: 30
            max_response_bytes: 100000      # hard cap keeps LLM context lean
        tools:                              # each tool -> one MCP method on this server
          - name: get_order_status
            description: "Fetch the current status of an order by id."
            request:
              method: GET
              path: "/orders/{order_id}/status"
              args:
                - {name: order_id, in: path, type: string, required: true}
            response:
              project: ["status", "updated_at"]
          - name: cancel_order
            description: "Cancel an order."
            mutating: true                  # non-GET is opt-in; fails closed otherwise
            request:
              method: POST
              path: "/orders/{order_id}/cancel"
              args:
                - {name: order_id, in: path, type: string, required: true}

      - name: inventory                     # mount /mcp/inventory; its own connection/service
        rest:
          base_url: "https://inventory.internal.example.com"
          allowed_hosts: ["inventory.internal.example.com"]
          auth: { mode: none }
        tools:
          - name: check_stock
            description: "Return stock level for a SKU."
            request:
              method: GET
              path: "/stock/{sku}"
              args:
                - {name: sku, in: path, type: string, required: true}

      - name: defect_model                  # a custom ML serving endpoint (shaped body)
        rest:
          base_url: "https://ml.internal.example.com"
          allowed_hosts: ["ml.internal.example.com"]
          auth: { mode: static, secret_scope: ml, secret_key: token }
        tools:
          # Tier 2 (declarative + template): named args -> a shaped JSON body.
          - name: score_batch
            description: "Score a batch of feature rows."
            request:
              method: POST
              path: "/invocations"
              args:
                - {name: rows, type: array, required: true}
              body_template: '{"dataframe_records": {{rows}}}'   # {{arg}} substitution only
            response:
              project: ["predictions"]
        # Tier 3 (code tool): for shapes needing computation (e.g. base64-encode
        # an image, derive fields). Dropped in as instance-side Python that
        # implements the payload's tool contract — see Authoring model below.
        code_tools:
          - module: tools/encode_and_score.py   # instance-relative; synced into the app
```

Paired attachment — **one `server.yml` per API** (the only thing the agent, or
any MCP client, sees). Because each API is its own MCP Service, tools are
namespaced by the service, not by a shared `name_prefix`:

```yaml
# instances/<name>/mcp_servers/orders/server.yml
type: mcp_service                            # new type -> /ai-gateway/mcp-services/{3-level}
mcp_service: "${var.rest_mcp_service__orders}"
tools:
  allow: ["get_order_status", "cancel_order"]
```

### Authoring model: declarative-first, with a contract-bound code tier

Pure declaration cannot cover *every* REST shape — the transport mapping
(endpoint / method / auth / arg placement) is universally declarable, but a
truly bespoke **input transformation** (base64-encode an image, derive a field,
custom batching) is not, without inventing a mini-language. So the config
service is **tiered**; a tool climbs only as high as it needs to:

| Tier | Use when | Authored as |
|---|---|---|
| **1 — flat declarative** | path/query/simple body | `args[]` |
| **2 — declarative + template** | shaped JSON body (most ML serving: `inputs` / `dataframe_records` / `dataframe_split`, tensors) | `args[]` + `body_template` (+ optional `input_schema`) |
| **3 — code tool** | the input needs *computation*, not just structure | `code_tools[].module` → an instance-side Python file |

The layering principle stands because these operate at **different levels** —
and this resolves the "instance = pure config?" tension:

- **The payload (Layer 2) grows the *vocabulary*.** Power users extend *what can
  be declared or plugged in* — new auth modes, new transforms, the OpenAPI
  importer, and **the code-tool contract itself** — following the upstream
  Agent-Foundry contracts. This is the "add to the payload" path, for anything
  **reusable across instances**.
- **The instance (Layer 3) *uses* that vocabulary.** It declares its tools in
  YAML and, for the bespoke long tail, drops in a Python **code tool** that
  *implements the payload's tool contract*. That is config-as-code specific to
  one integration — the same category as the agent payload's instance-authored
  `skills/`, not framework machinery. The rule the instance never crosses:
  **declare / implement a contract; never re-implement the machinery** (deploy,
  transport, auth plumbing, app host).

**Workflow decision rule** (so authors self-route): *reusable capability or new
mode?* → extend the **payload**. *Specific to this one endpoint?* → an
**instance** code tool.

Two things keep the code tier honest rather than an unbounded hole:

- **Feasibility is free.** `deploy_on_host` already syncs the whole
  `instances/<name>/` dir into the container (`sync_source`), so an
  instance-authored `tools/*.py` is *already present* at runtime — the payload
  runtime just discovers and mounts it onto that API's FastMCP server.
- **Rails still apply.** A code tool implements a **payload-owned contract**
  that injects the vetted primitives — the authenticated HTTP client bound to
  `rest.auth`, the `allowed_hosts` egress guard, and the `max_response_bytes`
  cap. A bespoke tool cannot bypass them, so security review stays bounded to
  "what does this function do with the injected client", not "did it reinvent
  auth". Sketch of the contract an instance file implements:

```python
# instances/<name>/payloads/rest_mcp/tools/encode_and_score.py
from rest_mcp import tool, HttpClient   # payload-owned contract (Layer 2)

@tool(name="score_image", description="Classify an image by URL.")
async def score_image(image_url: str, *, http: HttpClient) -> dict:
    # `http` is pre-bound to rest.auth + egress guard + byte cap.
    img = await http.get_bytes(image_url)          # rails enforced
    return await http.post_json("/invocations",
                                {"inputs": [{"image_b64": b64(img)}]})
```

**Templating (Tier 2) is deliberately restricted:** `{{arg}}` placeholder
substitution into a JSON `body_template`, validated at deploy time against the
declared `args` + optional `input_schema` (which is what the MCP card
advertises). No conditionals or loops — anything needing real logic has the
Tier-3 code path, so the template language stays a safe, deterministic
single-source-of-truth mapping.

### Best practices baked into the contract

Each borrows a pattern already proven in this repo, so there are no unicorns:

1. **Strict `ConfigDict(extra="forbid")`** on every spec model (newest
   convention, see `contracts/vector_search.py`) — a typo'd key fails fast.
   Do **not** copy `mcp.py`'s legacy `extra="ignore"`.
2. **Fail-closed, mirroring the MCP allowlist ethos:** `mutating: true` required
   for non-GET; `allowed_hosts` + https-only egress; `max_response_bytes` cap;
   unknown arg shapes (nested objects, enums, multiple bodies) rejected *loudly*
   until OpenAPI import lands (YAGNI now).
3. **Auth as a discriminated union on `mode`** (`none | static | obo`) —
   extensible without breaking existing configs. Secrets are referenced by
   scope/key only, never inlined, never logged. **Default to `static`/`none`:**
   `obo` to a *non-Databricks* REST API is
   unsupported (no dynamic client registration, U2M won't auto-prompt, OBO
   targets Databricks-ecosystem resources only), so gate `obo` as advanced and
   Databricks-target-only.
4. **Reuse the agent MCP naming taxonomy.** Adopt the `expose_as` aliasing model
   from `ToolsConfig` / `ToolAllowEntry` so naming a REST-proxied tool feels
   identical to allowlisting a Genie tool. (Cross-API `name_prefix` is no longer
   needed — each API is its own MCP Service, so its tool names are already
   namespaced by the service.) If both payloads want the shared arg/response
   primitives, factor them into `agent_foundry/contracts` — but only once there
   is a second consumer.
5. **Two hops kept distinct in the model.** The `spec` models *only* hop 2
   (`rest.auth`). Hop 1 (agent→app) is the UC connection / MCP Service
   provisioned at deploy time; the instance's `server.yml` references the service
   name. This is why "no agent-runtime changes" holds.
6. **Pure engine, declarative config.** `build_tool_specs(spec)` and
   `async call(tool, args)` import no FastAPI/agents — the `mcp-server-hello-world`
   split (`server/{app,main,tools}.py`). This is the surface power users extend.

### Two-hop auth

| Hop | Boundary | Owned by | Governance |
|---|---|---|---|
| 1 | agent → hosting app | UC HTTP connection + MCP Service (AI Gateway) | tool selection, service policy, rate limit, audit/usage/traces; **managed token + OAuth refresh — agent never sees tokens** |
| 2 | hosting app → REST API | the engine (`rest.auth.mode`) | `none` / `static` (Databricks secret) / `obo` (forward caller identity) |

Governance and hop-1 token handling are managed by UC / AI Gateway; the engine
owns only hop 2.

### Attachment: two strategies

The platform evolved (see above), so the governed default targets the current
**MCP Service** URL and the framework's legacy `type: external` builder
(`/api/2.0/mcp/external/{connection_name}`) is superseded. Two ways forward, a
deliberate trade-off:

| | **A. Governed (MCP Service)** — canonical default | **B. Direct (custom_app)** — smoke path |
|---|---|---|
| Attach as | new `type: mcp_service` → `{host}/ai-gateway/mcp-services/{cat}.{schema}.{svc}` (current governance URL) | existing `type: custom_app` → `{app_url}/mcp` (OBO) |
| Registration | HTTP connection + MCP Service (provision step) | none |
| Governance | AI Gateway: tool selection, policies, rate limits, audit, reuse by any client | Databricks Apps permissions only |
| Token handling | managed (OAuth refresh, agent never sees tokens) | OBO from the caller |
| Maturity | **Beta** (AI Gateway + Managed MCP previews) | GA-ish (documented custom-app path) |
| Framework change | add `mcp_service` server type + `/ai-gateway/mcp-services/{3-level}` URL builder in `contracts/mcp.py` / `build_canonical_url` | **none** (already supported) |

**Decision:** the payload runtime and engine are identical for both. Agents
consume through the connection substrate (**B**), which needs no Beta; **A** is an
optional governance layer on top (see
[As-built: optional MCP Service governance layer](#as-built-optional-mcp-service-governance-layer-path-c-beta)).

## Deploy lifecycle (rides the existing seam)

`agent-foundry deploy` finds the instance, `load_payload_config()` reads
`adapter: rest_mcp`, and `deploy_on_host(RestMcpPayloadAdapter(), ...)` runs the
identical App sequence. The adapter fills the seam:

- `build_payload_spec` → `AppPayloadSpec` (command = uvicorn/FastMCP entrypoint,
  env, `requirements_extras` for the lean server — no agent runtime / Lakebase).
- `build_infra` → `AppInfraSpec` (compute size; `secret` app-resources for
  `static` auth; OBO scopes for `obo`).
- `provision(ctx)` → after the App exists and its URL is known, for **each** API
  create/update the HTTP connection (host = `{app_url}/mcp/<api>`) and, under
  strategy A, the MCP Service; write `{app_url, connection, service}` per API to
  workspace-backed state.
- `load_state_outputs(ctx)` → republish `rest_mcp_service__<api>` /
  `rest_mcp_connection__<api>` (and `rest_mcp_url`) into `ctx.variables` so each
  paired `server.yml` resolves its `${var.rest_mcp_service__<api>}`.
- `plan_destroy` / `destroy` → tear down every connection / MCP Service + state;
  the App itself is removed by `destroy_on_host()`.
- `scaffolds()` → contribute `agent-foundry scaffold rest_mcp` (the manifest +
  paired `server.yml`), composed automatically into the CLI.

### As-built hop-1 auth (OAuth M2M via the App SP) + security note

The connection provisioning above targets, in the shipped MVP, the **external UC
connection** (strategy B substrate) rather than the MCP Service (strategy A,
deferred — see the separation note in
[payloads/rest_mcp/README.md](../../payloads/rest_mcp/README.md)). Its hop-1
auth is **OAuth M2M as the hosting App's own service principal**: U2M mapping was
rejected live because its browser authorization-code redirect URI is not
registered on the App's OAuth client and the workspace does not support dynamic
client registration.

Two consequences worth flagging for security review:

- **Deploy is more privileged than a code push.** `after_reconcile` mints a fresh
  OAuth secret for the App SP (`service_principal_secrets_proxy`) at
  connection-create time, so the **deploy identity must be allowed to mint SP
  secrets**. The secret value flows only into the UC connection's
  `options.client_secret`; **only the secret id** is persisted (workspace state)
  so `destroy` can revoke exactly that secret — a 1:1 connection↔secret lifecycle
  with no sprawl. The secret never touches logs or the variable bag.
- **Always tear down via the CLI.** Deleting the App by hand orphans the minted
  secret (valid until its default lifetime expires); `agent-foundry destroy`
  revokes it.

The connection's `base_path` carries a trailing slash (`/mcp/<api>/`) because the
App's Starlette mount 307-redirects `/mcp/<api>` and the managed proxy does not
follow the redirect.

### As-built: optional MCP Service governance layer (path C, Beta)

The MCP Service (strategy A / path C) is now implemented as an **optional,
opt-in-per-API governance layer**, cleanly separated from the connection
substrate. It lives entirely in its own `deploy/mcp_service.py` module and an
`McpServiceSpec` contract (an optional `mcp_service:` block on each API); the
connection module knows nothing about it, and it keeps a separate state
partition (`rest_mcp_service`). `after_reconcile` provisions connections first,
then — only for APIs whose block is enabled — builds the MCP Service *from* the
connection (create-if-absent + reconcile of tool selectors, service policies,
rate limits and `EXECUTE` grants); `destroy` tears services down before
connections.

The exact Beta `mcp-services` field shapes were **live-confirmed** and
are mapped from friendly config values in the module:

- connection reference is `connections/{metastore-fqn}` (bare connection name);
- rate limits use per-principal keys only (`RATE_LIMIT_KEY_{USER,USER_GROUP,SERVICE_PRINCIPAL}`,
  `principal` required, `minute` window) and the request count is the wire field
  **`tokens`** (the serving-endpoint `calls` field is silently ignored);
- service policies are flat `{name, policy_type: POLICY_TYPE_{BUILTIN,CUSTOM},
  handler}` — custom handlers are UC functions wrapped as `functions/{cat.schema.fn}`;
- invoke grants are `EXECUTE` via the `permissions/mcp_service/{3-level}` API.

**Govern-only for now.** Because agent consumption stays on the reliable
`type: external` connection, AI Gateway policy/rate-limit **enforcement** (which
only applies on the gateway URL) is not yet on the hot path — the surface is
provisioned and shelf-ready so a later phase can flip consumption to
`/ai-gateway/mcp-services/{3-level}` with config only. When the account previews
are off, the layer logs a warning and is skipped so the connection-only deploy
still succeeds.

**Verified end-to-end + a Playground consumption caveat (root-caused).** A live
deploy created the service from the connection and applied the
governance surface; every standards-compliant MCP client (Python MCP SDK,
Claude, `curl`) drove it through the gateway URL end-to-end (full `tools/call`
results). **AI Playground**, however, fails on **governed / non-ALLOW**
responses with *"SSE stream closed without a JSON-RPC response event."* Root
cause: the AI Gateway frames **ALLOW** responses as SSE but **DENY / ASK /
rate-limit-exceeded / unauthorized** as `application/json`, and the Playground
UI currently has an **SSE-only decoder** (behind `mcp.enableInlineElicitation`).
It is a Databricks Playground bug with UI + gateway fixes in flight, not a
payload defect — and it reinforces keeping consumption on the `type: external`
connection, which sidesteps the gateway's governed-response framing entirely.

## Instance layer & the portfolio system

- **Scaffold:** `agent-foundry scaffold rest_mcp` materialises a commented
  `payloads/rest_mcp/payload.yml` (every typed field present, `# yaml-language-server`
  schema header) plus a paired `mcp_servers/<api>/server.yml` per example API.
- **Wizard-selectable example variant.** Ship a self-contained, **proxy-only**
  variant under `examples/rest_mcp_proxy/` so `agent-foundry init` offers it in
  the starter menu next to `blank` / `portfolio_assistant`, and the user can
  select it to deploy *just* the proxy MCP. The wizard auto-discovers it — **no
  registry or `pyproject` edit** ([`examples_registry.py`](../../agent_foundry/init/examples_registry.py):
  *"drop a directory under `examples/<variant>/` with a `manifest.toml`"*) —
  provided the directory carries **both** `manifest.toml` (`display_name` +
  `description`) **and** `__init__.py` (packaging discovery requires both, per
  [`examples_variants.py`](../../agent_foundry/packaging/examples_variants.py);
  the parent `examples` package-data glob then bundles it into the wheel). The
  variant contents: own `foundry.toml`, **no `agent/` dir**, a
  `payloads/rest_mcp/payload.yml` wrapping a **public no-auth JSON API**
  (`auth.mode: none`), and paired `mcp_servers/<api>/server.yml` — so it deploys
  only the hosting App + registration with zero secrets. This is the
  "meaningful scaffold in the portfolio system" the design calls for, and it
  must render through the init renderer + pass the CI variant-discovery /
  packaging drift guards.
