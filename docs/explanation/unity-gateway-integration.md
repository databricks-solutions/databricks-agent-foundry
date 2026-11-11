# Unity Gateway integration — spike findings

> **Status: findings.** Live checks run on 19 October 2026 against a
> Databricks workspace as a user (OAuth U2M), with
> `databricks-sdk` 0.154.0, `databricks-openai` 0.15.0 and CLI v1.21.0
> as the latest releases. They decide how the agent payload consumes
> Unity Gateway model services, MCP services and skills. Checks that need
> a deployed App or the App service principal are covered by the
> `gateway_assistant` canary, not here.

## Results

| # | Check | Result |
|---|-------|--------|
| 1 | List MCP and model services in `system.ai` as a user | Works. `w.ai_gateway.list_mcp_services(parent="schemas/system.ai")` returns 11 services (dbsql, genie_one_mcp, web_search, github, gmail, slack, atlassian, google_calendar, google_drive, microsoft_365, sandbox). `list_model_services` returns the model catalog (e.g. `system.ai.claude-sonnet-5-5`). Service-principal listing: canary. |
| 2 | Chat through `/ai-gateway/mlflow/v1` | Works with the existing client: `AsyncDatabricksOpenAI(use_ai_gateway=True)` (databricks-openai 0.15) sets that base URL, `model=<fqn>`. Plain and streaming work. Per-block `cache_control` works: a 17k-token system block was read from cache (`cache_read_input_tokens=17403`). Thinking: `thinking.type=enabled` is rejected for current Claude models (same rule as serving endpoints); `adaptive` plus `reasoning_effort` is accepted. Unknown `extra_body` keys are accepted silently, so request tags are not verified and are not relied on. |
| 3 | Apps `uc_securable` `MODEL_SERVICE` resource | SDK and CLI schema accept it (`CONNECTION`, `FUNCTION`, `MODEL_SERVICE`, `TABLE`, `VOLUME`). Deploy behavior: canary. |
| 4 | `tools/list` on a gateway MCP | Works at `{host}/ai-gateway/mcp-services/<fqn>` with `DatabricksOAuthClientProvider`: `system.ai.dbsql` → `execute_sql`, `poll_sql_result`; `system.ai.web_search` → `web_search`. App OBO scope `ai-gateway`: canary. |
| 5 | Grants | `w.grants.update(securable_type="skill", ...)` grants and revokes `READ_VOLUME`. The SDK needs `Privilege` enum members, not strings. `grants.get` works for `mcp_service` and `model_service`. Granting `EXECUTE` on an MCP service needs `MANAGE` on it: canary. |
| 6 | Per-user provider MCP (`system.ai.github`) | `tools/list` fails until the user signs in. Provider services are recognisable: `config.source_connection.options.oauth_provider` is set. `get_mcp_service_user_mapped_credential(name="mcp-services/<fqn>")` raises `NotFound` whose message carries the sign-in URL (`/explore/connections/<name>`). The sidebar shows "sign in required" with that link; such a service is never mounted without a credential. |
| 7 | Skill registry MCP `/ai-gateway/skills/?schema=<cat>.<schema>` | Exposes `list_skills(catalog, schema)`, `load_skill(full_name)`, `get_skill_files(full_name, paths)`, one tool per skill (`skill_<fqn>`), and `create_skill` / `update_skill` / `delete_skill` taking **local directory paths** (built for local coding agents). |
| 8 | Fetching skill bodies; etags | `w.files.download("/Skills/<cat>/<schema>/<id>/SKILL.md")` returns the same text as `load_skill`; `w.files.list_directory_contents` lists supporting files. `get_skill` returns an `etag`. **Every `finalize_skill` changes the etag, even with unchanged content.** |
| 9 | `AgentServer` routes receive `x-forwarded-access-token` | Canary (needs a deployed App). |
| 10 | Create → upload → finalize → delete as a user | Works with `USE CATALOG` / `USE SCHEMA` / `CREATE VOLUME`: `create_skill(Skill(comment=...), parent="schemas/<cat>.<schema>", skill_id=...)`, Files upload, `finalize_skill` derives `bundle_name` and `description` from the frontmatter, `delete_skill`. Owner and creator are the calling user. As the CI service principal: `ai_gateway_brick` canary. |
| 11 | Lakebase `agent_foundry` schema at Express startup | Canary (needs a deployed App with chat history). |
| 12 | Registry argument shape | `load_skill` and `get_skill_files` take `full_name` (`catalog.schema.name`). |

## Decisions

- **One LLM client.** The runtime keeps `PromptCachedDatabricksOpenAI` and
  passes `use_ai_gateway=True` for `model.kind: model_service`. Prompt
  caching works unchanged, so `model_service` does not need to stay
  experimental. Request tags are left out until a supported field exists.
- **Skills load through the SDK, not the registry MCP.** The registry MCP
  mixes read tools with write tools that take local file paths, plus one
  tool per skill, so mounting it would need a tool and argument filter. The
  runtime instead uses a native `load_skill` function tool that
  only accepts indexed FQNs and reads `SKILL.md` through the Files API with
  the configured identity. That is less code and keeps write tools
  unreachable by construction. The eager block uses the same download path.
- **The brick only finalizes on change.** Because finalize always rotates the
  etag, the `ai_gateway` brick compares a content hash with the deployed
  bytes and skips upload and finalize when they match; otherwise every deploy
  would invalidate the eager prompt-cache block.
- **Provider MCPs need a credential.** A selected provider MCP without a
  user credential is reported as unavailable with its sign-in link and not
  mounted.
