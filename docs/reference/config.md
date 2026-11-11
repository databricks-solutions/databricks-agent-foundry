# Configuration

Workspace-level configuration lives in your **application repo** (the
downstream consumer that pins `agent-foundry` as a library). The framework
itself ships **no** target configs -- it reads them from your project root
at runtime.

The authoring contract is two typed `foundry.toml` files plus a handful of
per-brick YAMLs. Older layouts are rejected (see
[Unsupported layouts](#unsupported-layouts) below).

## Typed config (`foundry.toml`)

A downstream repo authors two TOML files, typed by
`agent_foundry.public.foundry_config` (`FoundryRepoConfig` /
`FoundryInstanceConfig`). The split encodes an audience boundary:
operators own the repo-root file (workspace targets); agent authors own the
per-instance file (identity + feature toggles). Both use `extra = "forbid"`,
so a key in the wrong file fails at load time instead of being silently
ignored.

```
my-agent-app/
├── foundry.toml                       # repo-root: targets + instance list
├── instances/
│   └── portfolio-assistant/
│       ├── foundry.toml               # per-instance identity + warehouse binding
│       ├── app/config.yml             # generic host: compute, OBO, payload ref
│       ├── payloads/agent/payload.yml # adapter + spec (the single payload config)
│       └── ...
├── .agent-foundry/schemas/            # synced JSON Schemas (IDE support)
└── pyproject.toml                     # pins agent-foundry as a library
```

Repo-root `foundry.toml` (`FoundryRepoConfig`):

```toml
#:schema .agent-foundry/schemas/foundry-repo.schema.json
schema_version = 2

[targets.dev]
host    = "https://your-dev-workspace.cloud.databricks.com"
catalog = "dev_catalog"
schema  = "analytics"
profile = "your-cli-profile"

[[instances]]
path = "instances/portfolio-assistant"
```

Each `[targets.<name>]` maps to one Databricks workspace; pick it with
`--target dev|staging|prod` on every CLI call (`dev` is the default).
`warehouse_id` is **not** a target field as of `schema_version = 2` -- the
warehouse a given instance binds to is a per-instance concern.

Per-instance `instances/<name>/foundry.toml` (`FoundryInstanceConfig`):

```toml
#:schema ../../.agent-foundry/schemas/foundry-instance.schema.json
name             = "portfolio-assistant"
description      = "Agent with Genie Space analytics and Vector Search retrieval"
schema_prefix    = "portfolio_assistant"
vs_endpoint_name = "vs_docs_demo"

[warehouse]
mode = "dedicated"   # or "reuse" + warehouse_id = "..."
```

`name` and `schema_prefix` are required; `app_name` / `chat_ui_app_name`
derive from `name` when omitted. `vs_endpoint_name` is **shared** Vector
Search endpoint infrastructure (not derived from the instance name);
indexes still land in `{catalog}.{schema_prefix}_indexes` (or the instance
schema the index config names). Two instances may point at the same
endpoint. `schema_prefix` is the instance's Unity Catalog schema namespace
inside the target catalog; extra suffixes (`_views`, `_indexes`, `_agent`)
are only for instances that split concerns across several schemas. Runtime config and feature toggles are
**not** authored here — they all live once in the agent payload's
`payloads/agent/payload.yml` `spec` block (the single per-payload config
contract). `chat_history` defaults **on**; file upload defaults **off** (it
launches an untrusted-input parser, so enablement is a deliberate act) — see
the Agent App brick's
[File upload (extraction)](../../bricks/app/README.md#file-upload-extraction).

### Payload config (`payloads/agent/payload.yml`)

The framework knows only a generic envelope: `payload.adapter` (which
payload to run) plus an opaque `payload.spec` the adapter owns. The
framework never learns what an "agent" is. For the built-in `agent` adapter,
the `spec` is typed by `payloads.agent.contracts.AgentPayloadSpec` and split
into three concern-separated sections:

- **`spec.runtime`** — what the agent executes (the model as `model: {kind,
  name}` with `kind` `serving_endpoint` or a Unity Gateway `model_service`),
  system prompt, limits,
  `tools`: `mcp_only`, inline `definitions`, the skills under
  `tools.skills`, the MCP servers,
  and `context`: what fills the context window and how it is cached).
  MCP servers mount on presence: `mcp_servers/*/server.yml` files mount
  workspace servers and a `tools.mcp.gateway` block mounts Unity Gateway MCP
  services next to them; workspace tool names win a collision.
  `tools.mcp.source` is deprecated and only warns. Skills come from three
  branches, each with an `enabled` toggle (see the example below):
  - `skills.repository` (on by default): the payload's `skills/` folder. With
    `enabled: false`, `skills/` content left in place fails
    `agent-foundry validate` and deploy.
  - `skills.unity_catalog` (on once the block is present): `pinned` skills
    (`identity: sp` or `obo`) and `discovery` locations read as the app. It may
    sit next to repository skills; their skills come first in the prompt, and
    deploy rejects a skill folder named like a pinned skill.
    `refresh_seconds` (default 900) sets how often the app re-checks the
    service-principal skills for edits. Only a changed skill is downloaded
    again, and unchanged skills render the same bytes, so the prompt cache
    only busts on a real edit or a change to the skill set. With lazy loading
    only the index is refreshed. `0` reads the set once per process.
  - `skills.user_skills`: each user's own skills, `enabled` plus one `store`:
    `lakebase` or `unity_catalog` (with `locations`, `include` / `exclude`
    and `authoring_schema`).

  `skills.source` and `skills.gateway`, and `user_skills.store` without
  `enabled`, are deprecated: they still work, deploy warns, and mixing them
  with the new branches is a validation error. `source: unity_gateway` reads
  as `repository.enabled: false`; `gateway.pinned` / `discovery` /
  `refresh_seconds` as `unity_catalog`; `gateway.user_catalog` and
  `authoring` as `user_skills.store: unity_catalog`. `loading: lazy` turns
  into eager for every source while `context.prompt_cache` is on.
- **`spec.ui`** — the presentation surface, its feature toggles
  (`chat_history`, `file_upload`, `feedback`, `charts`, `resources_sidebar`,
  `speech_input`)
  and presentation modes (`tool_output`, `chat_layout`).
- **`spec.integrations`** — exposing the agent to other systems (`a2a` card,
  `mcp_server`).

```yaml
# instances/<name>/payloads/agent/payload.yml
# yaml-language-server: $schema=../../../../.agent-foundry/schemas/agent_payload.schema.json
payload:
  adapter: agent
  requires: [warehouse, tracing, observability]
  spec:
    runtime:
      model:
        kind: serving_endpoint
        name: "databricks-claude-sonnet-4-5"
      system_prompt: "You are a helpful assistant with access to tools."
      max_tool_calls_per_turn: 5
      max_iterations: 50
      inject_time_user: true
      tools:
        mcp_only: true           # attach tools exclusively via MCP servers
        skills:
          loading: eager         # or "lazy" (skill index + _load_skill)
          repository:            # developer: the payload's skills/ folder
            enabled: true        # default
          # unity_catalog:       # developer: Unity Catalog skills, next to skills/
          #   pinned:
          #     - fqn: "cat.sch.sql-runner"
          #       identity: sp   # or obo
          #   discovery: {enabled: true, locations: ["cat.sch"]}
          #   refresh_seconds: 900
          # user_skills:         # each user's own skills in the panel
          #   enabled: true
          #   store: lakebase    # or unity_catalog (+ locations, include, authoring_schema)
      context:
        prompt_cache:
          enabled: true          # provider prompt caching (Claude endpoints)
          static_ttl: "1h"       # "5m" or "1h"; tools + system-prompt prefix
          # rolling_ttl: "5m"    # omit to use the endpoint default
        # data_universe:         # off by default; needs a UC function
        #   enabled: true
        #   function: "get_data_landscape"
        #   refresh_minutes: 60
        tool_result_clearing:
          enabled: true          # client-side stale tool-output clearing
          keep_tool_uses: 3      # recent rounds of tool calls kept verbatim
          clear_at_least_chars: 300000     # savings-available trigger
          clear_above_input_tokens: 150000 # occupancy trigger (in-turn emergency valve)
        # compaction:            # on wherever chat_history is on
        #   trigger_input_tokens: 120000   # must stay below clear_above_input_tokens
        #   keep_recent_turns: 3           # recent user turns kept verbatim
        #   instructions: "Keep ticket IDs."  # appended to the summary prompt
        #   manual_action: false           # "Compact conversation" in the header
        # plan:                  # per-chat goal and milestones; off by default
        #   enabled: true                  # needs chat_history
        #   max_chars: 8000                # largest plan the agent may save
        # memory:                # long-term memory; off by default
        #   enabled: true                  # the user's facts, across chats; needs chat_history
        #   max_entry_chars: 8000          # largest topic the agent may save
        #   index_max_entries: 50          # topics listed at each request
      # reasoning:               # thinking models; unset = endpoint default
      #   thinking: adaptive     # Claude 5.x: adaptive only (enabled/disabled 400)
      #   display: summarized    # readable reasoning; the default while emit is on
      #   effort: medium         # sent as reasoning_effort; scales Claude 5.x thinking
      #   emit: true             # stream reasoning on /invocations
      # model_settings:
      #   extra_body: {max_tokens: 16000}  # raw request-body passthrough
    ui:
      chat_history: {enabled: true}    # sidebar_days: 30; retention_days: 365
      file_upload:  {enabled: false}   # max_mb: 10; images: true for vision models
      feedback:     {enabled: true}
      charts:       {enabled: true}
      tool_output:  {expanded: false}  # tool-call panels start collapsed
      chat_layout:  {width: 4xl}       # chat column cap: 4xl | 5xl | 6xl | 7xl
      resources_sidebar: {enabled: false}  # Unity Gateway resources panel
      # speech_input:            # microphone button; off by default
      #   enabled: true
      #   endpoint: ${var.serving_endpoint_name__speech}  # scaffold speech_input
      #   max_seconds: 300       # 1..300
      #   language: en           # unset = detect per recording
    integrations:
      a2a:        {enabled: false}     # expose an A2A agent card
      mcp_server: {enabled: false}     # expose the agent AS an MCP server
```

**`ui.chat_history`** keeps chats in Lakebase (on by default). Two day counts
bound it; set either to `null` to turn it off:

- `sidebar_days` (default 30): the sidebar lists only chats created in the
  last N days. Older chats are not sent to the browser but still open by URL
  (`/chat/<id>`) until retention removes them.
- `retention_days` (default 365, must be >= `sidebar_days`): the chat UI
  server deletes chats, with their messages and votes, whose last message is
  older than N days, so a chat still in use is kept. It sweeps a minute after
  start and then daily, at most 10,000 chats per sweep, and logs only the
  number deleted. MLflow traces and feedback are not touched; they follow the
  experiment's own lifecycle. Existing deployments start deleting chats older
  than a year on their next deploy unless they set `retention_days: null`.

**`ui.file_upload.images`** (off by default, needs `ui.file_upload.enabled`)
lets users paste, drop or attach PNG, JPEG and WebP images. The browser scales
each image to at most 1568 px on the longest edge (same format; JPEG and WebP
at quality 0.85) and keeps the original if that is smaller; the result must be
under 5 MB. Images skip the text extractor and reach the model as image
content, so the agent's model must accept images (for example Claude, GPT-5 or
Gemini); a text-only model makes the agent answer with a notice to start a new
conversation without the image. The image is stored inline in the chat message
and is re-sent with every later turn of that chat.

**`ui.resources_sidebar`** adds a **Resources** panel to the right of the chat.
The panel has three sections:

- **Agent:** the resources the agent always uses, read-only. These are the
  gateway `pinned` entries and what `discovery` finds, or the `server.yml` MCP
  servers and Foundry skills. Each entry shows whether it runs *as you* or
  *as the app*.
- **Your picks:** the resources you have added. These are sent with each of
  your messages as `custom_inputs.selected_resources`.
- **Add from your catalog:** what your own grants let you see in each enabled
  `user_catalog` block, filtered by that block's `include` / `exclude`.

More details:

- **Saving:** a pick is saved as soon as you toggle it. With chat history
  enabled it is stored in Lakebase (`ai_chatbot.user_resource_prefs`, created on
  first use); otherwise it is kept in memory until the app restarts.
- **Limits:** the panel stops offering adds at the `user_catalog` limits. The
  agent re-checks every pick against the policy and limits on every request, so
  a stale or edited pick can never widen what it uses.
- **Tools:** expanding an MCP entry lists its tools, connecting as the identity
  the agent would use.
- **Endpoints:** the panel reads `GET /api/agent/resources` and
  `GET /api/agent/resources/tools?fqn=`, which are mounted only while the toggle
  is on (`ENABLE_RESOURCES_SIDEBAR`).
- **Your own skills:** with `skills.user_skills.store: unity_catalog` and an
  `authoring_schema`, users can create, edit and delete skills there through
  `POST /api/agent/skills` and `PUT` / `DELETE /api/agent/skills/{fqn}`. These
  calls always run as the user and fail with 401 without a user token; the app's
  service principal is never used. A skill is named `u-<handle>-<slug>`, where
  `<handle>` is the user's email local part in lower-case hyphen form, and its
  `SKILL.md` must stay under `user_skills.max_skill_md_chars`. Unity Catalog
  decides who may edit or delete a skill, and its 403 or 409 is passed back
  unchanged. The panel offers a **New skill** form for creating one; the new
  skill is picked straight away when the skill limit leaves room.

  **Not available inside Databricks Apps yet:** the skills API requires the
  `unity-catalog` OAuth scope, which Apps cannot grant. In an App the routes
  answer 501 (`apps_unity_catalog_scope`) and the panel shows the form greyed
  out with that reason.

  Admin prerequisites: create the schema, list it in `user_skills.locations`
  (validation requires this), and grant authors `USE CATALOG` on the
  catalog plus `USE SCHEMA` and `CREATE VOLUME` on the schema.
- **Your own skills in Lakebase:** set `tools.skills.user_skills.enabled: true`
  with `store: lakebase` to let each user write, edit,
  delete and pick their own skills in the panel, also inside Databricks Apps.
  This needs `ui.chat_history` and `ui.resources_sidebar` to be on. The
  payload scaffold turns all three on in new projects; `agent-foundry smoke`
  then also checks that the agent loads a user skill.
  - Skills are stored in the chat-history Lakebase (`ai_chatbot.user_skills`,
    created on first use), keyed by the signed-in user. Only that user can
    read, edit or delete them, and they are kept until the user deletes them.
  - A skill is named by a slug (lower-case letters and digits, single hyphens,
    at most 40 characters) and appears as `lakebase:<slug>`. Each user may
    have at most `max_skills` (default 20) skills, each body at most
    `max_skill_md_chars` (default 20,000) characters. Deleting a skill also
    removes its pick.
  - A skill created in the panel waits under **Add from your catalog** until
    the user adds it to their picks; removing a pick (**−**) puts it back
    there.
  - Picked skills are sent with each message as `custom_inputs.user_skills`.
    The agent re-checks their shape, count and size, lists them by name and
    description after the cached prompt prefix, and loads a body through
    `_load_skill` when the model asks for it. The tool is registered even
    when nothing is picked, so prompt caching is shared across users. Traces
    record skill names and body hashes, never the text.
  - The chat UI server serves the routes (`GET /api/user-skills`,
    `POST /api/agent/skills`, `PUT` / `DELETE /api/agent/skills/{fqn}`); the
    agent server does not mount its Unity Catalog authoring routes then.
  - The agent can create or update the user's skills with its `_save_skill`
    tool (slug, description, body), for example when the user asks it to
    turn a conversation into a skill. It cannot delete them. The tool checks
    the draft and tells the model what to fix; the chat UI server then saves
    the skill as the signed-in user after the response, picks it, and shows a
    toast with the outcome. Because the save happens after the tool returns,
    a limit error reaches the user's toast, not the model. Saves are skipped
    when the chat falls back to non-streaming generation.

    The trade-off: content the agent reads, such as a document or a tool
    result, could steer it into saving a skill, which is picked and applies
    from the next message. The toast makes every save visible, and the user
    can edit, unpick or delete the skill in the panel.

**Data behind `sp` MCP services.** Services that run SQL as their caller, such
as `system.ai.dbsql` and `system.ai.genie_one_mcp` pinned with `identity: sp`,
see exactly what the app's service principal is granted. The `system.ai`
services themselves need no grant. Grant the data on the app brick, in the
instance's `app/config.yml`, the same way the scaffold grants the trace tables:

```yaml
# app/config.yml
app_resources:            # Apps-native: one table per entry
  - name: "sales-orders"
    uc_securable:
      securable_full_name: "${var.catalog}.sales.orders"
      securable_type: "TABLE"
      permission: "SELECT"
sp_grants:                # SQL GRANT after the app exists: a whole schema
  - securable_type: "SCHEMA"
    securable_name: "${var.catalog}.sales"
    permissions: ["USE SCHEMA", "SELECT"]
```

An instance key replaces the scaffold's key of the same name, so copy the
scaffold's `app_resources` / `sp_grants` entries into the instance file before
adding your own.

**`context.prompt_cache`** marks the stable prefix of each request so the
provider bills it as a cache read (0.1x) rather than fresh input. Two things are
worth knowing before tuning it. The TTL is a *sliding window* refreshed for free
on every hit, so a breakpoint stays warm as long as traffic keeps touching it —
a full-price write is paid only when the prefix goes cold or its bytes change,
not once per TTL period. The TTLs are a closed set (`"5m"` / `"1h"`) because an
unrecognised value would be accepted by the gateway and turn every request into a
full-price write.

Caching is cumulative from the start of the prompt, so each request is ordered by
how often its parts change: the cached system prompt; then a `system` message with
the per-user skill index and the unavailable-tools note; then the conversation
history; and **last**, a `system` message with what changes between turns — the
identity from `inject_time_user` (user and UTC time to the minute), the memory
index and the open plan. Nothing after the history is cached, so the time, a new
plan step or a new memory note costs only that last message, and the whole history
is still read from cache. Both extra messages exist only in the request; nothing
is written into the conversation history.

**Prompt caching and `tools.skills.loading: lazy` are mutually exclusive, and
caching wins.** A deploy that finds both warns and assembles eager anyway. Lazy
loading is not merely redundant under caching but actively worse: skill bodies
arrive as *tool results* in the conversation, behind the volatile tail, so they are
re-sent and re-written at full price on every request instead of riding the cached
prefix — and each activation costs an extra LLM round trip. Measured on a
multi-skill question, lazy cost 3x what eager cost. The intent behind `lazy` — a
smaller prompt — is better served by a cached large prompt than an uncached small
one. To keep lazy loading, set `context.prompt_cache.enabled: false`.

**`context.tool_result_clearing`** replaces older tool outputs with a short
placeholder immediately before each model call. The full chat transcript in
Lakebase is untouched — only the in-flight request sent to the model is
rewritten — so this works the same on Claude, GPT and open-weights endpoints
served through the OpenAI-compatible gateway. It is **on by default**
(`keep_tool_uses: 3`) because long conversations otherwise re-send every tool
result on every loop iteration. Tool calls and their arguments are preserved;
only bulky `function_call_output` payloads are cleared. `keep_tool_uses` counts
rounds, not results: the results of everything one model call asked for, in
parallel or not, count as one. Only rounds with something to clear count, and
the outputs of the built-in plan, memory and `_load_skill` tools are never
cleared, so plan updates between queries do not push recent query results
out. An output the model has
not read yet — such as a fresh batch of parallel results — is never cleared,
whatever `keep_tool_uses` says. The placeholder tells
the model it may re-call the tool if it needs the data again. Set
`enabled: false` only as a break-glass switch while debugging a regression.
Clearing arms on **either** of two independent thresholds, because neither
alone covers every conversation shape.

`clear_at_least_chars` (default 300,000) is the *savings-available* trigger: it
delays clearing until replacing all eligible old results recovers at least that
many characters. Characters are used instead of provider-specific token counts
so the policy behaves identically across model families. The default is high
deliberately. Clearing rewrites `function_call_output` items in the *middle* of
the message list, so the prompt-cache prefix breaks at the first cleared item
and everything after it re-bills as a write instead of a 0.1x read — measured on
one 9-turn session, a low threshold cost roughly 90% cache efficiency to recover
at most 26K tokens on a window that never went past 37% full. Lower it only if
you are deliberately trading cache discount for a smaller prompt.

`clear_above_input_tokens` (default 150,000) is the *occupancy* trigger, and the
in-turn emergency valve: it clears once the outbound request is estimated to carry
that many prompt tokens, however little the pass recovers. Compaction is the
normal valve, but it only runs between turns, so within one tool-heavy turn this
is the only bound. It clears every eligible output at once, and a cleared output
stays cleared for the rest of the turn; the next pass runs only once the
request, with those outputs cleared, is over the threshold again. Between passes
every call sends the same prefix, so a pass breaks the prompt cache once instead
of on every later call. It exists because
savings-available cannot express the case that matters — a conversation whose
growth is prose rather than tool output never accumulates enough clearable
material to trip any savings threshold, and nothing else in the framework bounds
context growth, so it would reach the provider's limit and fail with
`prompt is too long` — and every retry replays the same oversized history, so
the turn cannot succeed. The runtime now answers that rejection with a notice
telling the user to start a new conversation, rather than surfacing the raw
provider error, but the turn is still lost: the occupancy trigger exists so it
does not get that far. The estimate is a character count over the message list, the
instructions *and* the tool definitions (every MCP and inline tool's name, description
and JSON schema, sent with every call), divided by a conservative characters-per-token constant, so it
needs no tokenizer and errs toward arming early. The default suits the 200K
window of the Claude endpoints; **lower it for a smaller window**, and set `0`
to disable the trigger.

Because clearing only ever reaches tool output, it cannot bound a conversation
whose bulk is prose. When the occupancy trigger fires and the request is still
over the threshold afterwards, the runtime logs a warning — that is the signal
that this agent needs shorter turns or a fresh conversation, since no clearing
setting will help.

**`context.compaction`** handles that prose case. Once a turn's last request is
estimated above `trigger_input_tokens`, measured before tool-result clearing
(the size the next turn starts from), the chat UI server summarizes everything
before the last `keep_recent_turns` user turns in the background, stores the
summary in Lakebase next to the chat, and sends it in place of those turns from
then on. The chat still shows every message, with a divider where the summary
starts. A ring next to the send button shows how close the conversation is to
`trigger_input_tokens`, and a short notice appears while the summary is written.
`manual_action: true` adds a header action to compact on demand with an
optional focus; it is off by default. Compaction is **on
wherever `ui.chat_history` is on** and unavailable without it; setting
`enabled: true` with chat history off is a validation error.
`trigger_input_tokens` must stay below `clear_above_input_tokens`: compaction is
the normal valve between turns, clearing the emergency valve within one; unset,
it is 120,000 or the clearing threshold minus 20,000, whichever is smaller. `instructions` (at most 2,000 characters) adds
instance-specific rules to the fixed summary prompt. See
[Conversation compaction](../explanation/conversation-compaction.md).

**`context.plan`** is **off by default**. With `enabled: true` the agent gets
`_update_plan` and `_delete_plan`, which keep a goal and outcome milestones for
multi-step work in one chat. While a milestone is open the plan is recited to
the model before every model call, together with a one-line summary of each
goal already finished in the chat. The chat owner can follow it in a collapsed
panel above the chat input, which shows each milestone as open, done or dropped
and refreshes when the agent updates the plan. The plan is deleted with its chat. It needs
`ui.chat_history` (setting `enabled: true` with chat history off is a
validation error), and MCP and A2A callers never get it, since they have no
conversation. `max_chars` (default 8,000) caps the saved plan.

**`context.memory`** is **off by default**. With `enabled: true` the agent gets
`_remember`, `_recall`, `_forget` and `_list_memories`, which keep one-line
facts about the signed-in user and their environment, grouped by topic, across
all of their chats. The user's topics are listed at the start of each request.
Memory is kept per user in the chat-history Lakebase, so it needs
`ui.chat_history` (setting `enabled: true` with chat history off is a
validation error); it needs no conversation and is available to MCP and A2A
callers. `max_entry_chars` (default 8,000) caps one topic.

**`context.memory_store`** was removed with the managed memory store it named;
a payload that still sets it fails validation. The keys `memory.store`,
`memory.plan`, `memory.user` and `memory.chat` were removed too; a payload that
still sets them fails validation with a message naming the replacement.
See [Agent memory and plans](../explanation/agent-memory.md).

**`context.data_universe`** is **off by default**. Set `enabled: true` to
read a Unity Catalog table function — one row per view the agent can query, with
its location, size and description — and render it into the system prompt, so
the agent knows what data exists without spending a tool call to find out. Any
other field in this block requires `enabled` to be set explicitly. It rides in
the cached prefix, which is what makes a multi-thousand-token catalog listing
cheap enough to carry on every request.

The SQL stays adjustable after deployment, which is the point of putting it in a
UC function rather than in Python: the runtime only ever issues
`SELECT * FROM <function>()` and renders whatever columns come back, so changing
the projection, the descriptions or the row order is a `CREATE OR REPLACE
FUNCTION` through the `sql_function` brick — no app redeploy. `refresh_minutes`
(default 60) governs how soon the runtime notices such a change. Re-reading is
cheap: the cache is keyed on bytes, so an unchanged block re-renders identically
and keeps hitting, and a write is paid exactly when one is wanted — after the
content actually changed.

Two constraints worth stating plainly. **The read uses the app's service
principal, not the signed-in user**, because the block must be byte-identical for
everyone to be cacheable at all; the function must therefore expose only metadata
every user of the app is cleared to see. Enabling the block grants that service
principal `EXECUTE` on the named function as an app resource at deploy time — the
grant is derived from this config, so there is nothing to author in
`app/config.yml` and nothing to keep in sync. And the block is treated as untrusted
input — view comments are authored by whoever owns the views, so the block is
labelled as reference data rather than instructions, and every cell is forced onto
one line with control characters, fence sequences and pipes neutralised. A failed read is never fatal:
the last good block is served, or none at all, and the agent answers without it.

**`reasoning`** configures thinking models such as `databricks-claude-sonnet-5-5`.
Every field is optional, and an unset field sends nothing: the endpoint's own
default applies, and a non-thinking model gets the same request as before.
`thinking` is sent as `thinking: {type: ...}`; `budget_tokens` (required with
`thinking: enabled`, and only allowed there) rides along in it; when
`model_settings.extra_body.max_tokens` is set, it must be an integer larger than
the budget. `effort` is sent as `reasoning_effort`. Combinations an endpoint is known
to reject fail validation at load time; beyond those, which values an endpoint
accepts is the endpoint's call. Claude 5.x on Databricks (FMAPI serving names
like `databricks-claude-sonnet-5-5` and Unity Gateway `system.ai.claude-sonnet-5-5`)
rejects `thinking: enabled` and `thinking: disabled`: use `thinking: adaptive` and
`effort` (the completion budget is `model_settings.extra_body.max_tokens`; there is
no `budget_tokens` on that family). The endpoint error text also names
`output_config.effort`; the chat-completions path takes `reasoning_effort`.

`display` is sent inside the thinking block and decides whether the reasoning comes
back as readable summary text (`summarized`) or empty (`omitted`). Newer models
such as Sonnet 5.5 return it empty unless asked, so the runtime sends
`display: summarized` by default for `thinking: adaptive` or `enabled` while `emit`
is on; `thinking: adaptive` alone is enough to see reasoning. Set
`display: omitted` to opt out. With `emit: false` nothing is requested, so set
`display: summarized` explicitly if you still want the summaries in MLflow traces.
`display` needs `thinking` to be set, and is rejected with `between_tools` (the
endpoint returns a 400) and with `disabled`. If `thinking` is unset, no thinking
block is sent at all, so on Sonnet 5.5 the reasoning stays empty.

When the model thinks, Databricks returns the message content as a list of
reasoning and text blocks. The runtime folds the text into the answer and the
reasoning summary into a reasoning item, which `/invocations` streams as
`response.reasoning_summary_text.delta` events plus a `reasoning` output item; the
chat UI renders it as a collapsible section. `emit: false` drops those events for
the whole deployment, and a caller can decide per request with
`custom_inputs: {"include_reasoning": false}` (or `true`; the strings `"false"` and
`"true"` work too). Signatures are returned only to a caller that opts into
reasoning replay (below).

Within one agent run, the signed reasoning blocks are sent back unmodified on the
assistant message that made the tool calls, as Databricks recommends for multi-step
reasoning. The OpenAI Agents SDK drops them on its chat-completions path, so the
runtime keeps them, keyed by tool-call id, for as long as the run lasts. Each run
has its own copy, so nothing crosses between conversations or users.

Earlier user turns come from the caller's history, which carries no signatures.
Claude keeps earlier turns' thinking in the prompt, so without them the prompt cache
would stop matching at the first earlier assistant message and the whole history
would be written to the cache again on every turn. A caller that sends
`custom_inputs.reasoning_replay` (a list; an empty one opts in) gets the run's
original content back in `custom_outputs.reasoning_replay`, one entry per model call
that made tool calls (`model`, `tool_call_ids`, `content`), and sends the entries
with later turns. The runtime seeds the run with them and puts each back only on the
assistant message with that tool-call id, for the same model and with the same text.
It accepts only signed reasoning and text blocks, at most the last 500 entries, and
skips anything else. A model call that made several tool calls without signed
reasoning gets an entry with empty `content`: it only records which calls belong
together, so the runtime can put them back into one assistant message (the chat
history stores each call next to its result). Nothing is exported while reasoning is off (`emit: false` or
`include_reasoning: false`). The chat UI opts in whenever it has its Lakebase: it
keeps the entries in `ai_chatbot.chat_reasoning_replay`, deleted with the chat or
the message they came with, sends back those for the tool calls still in the request
after compaction, and never sends them to the browser.

**`model_settings.extra_body`** is merged into every chat-completions request body
as authored, for endpoint parameters the typed blocks do not cover (`max_tokens`,
`temperature`, ...). Keys that something else owns — `model`, `messages`, `tools`,
`tool_choice`, `stream`, `stream_options`, `thinking`, `reasoning_effort` — fail
validation with a pointer to where they belong.

Every `spec` model uses `extra = "forbid"`, so a misspelled or misplaced
key fails fast at deploy time. Tool *providers* (the `skills/` and
`mcp_servers/` directories, the `tools.definitions` list) are enabled by
*existing* — they carry no duplicate `enabled` toggle; only their
behavioural modes live in `spec`.

Inspect the fully-resolved view with
`uv run agent-foundry config show --target dev --instance portfolio-assistant`.

## Editor schemas (`.agent-foundry/schemas/`)

The framework ships a JSON Schema for every typed config file **inside the
wheel**, generated from the Pydantic contracts. Copy them into your repo so
editors (VS Code's `yaml-language-server`, PyCharm, ...) autocomplete and
validate as you type:

```bash
uv run agent-foundry sync-schemas
```

This writes `.agent-foundry/schemas/*.schema.json`. Reference a schema from
the top of each file:

```toml
# foundry.toml
#:schema .agent-foundry/schemas/foundry-repo.schema.json
```

```yaml
# instances/<name>/payloads/agent/payload.yml
# yaml-language-server: $schema=../../../../.agent-foundry/schemas/agent_payload.schema.json
```

The `agent_payload.schema.json` is **payload-owned** (generated by
`payloads/agent/schemas/_generate.py`), not part of the framework schema
bundle — the framework stays agent-agnostic.

Re-run `sync-schemas` after every framework upgrade so the on-disk schemas
track the installed wheel. Because the schemas are deterministic projections
of the models, CI fails if a model changes without regenerating them -- see
`agent_foundry/schemas/_generate.py` and `agent_foundry/tests/unit/test_foundry_schemas.py`.

## Variable resolution order

`foundry.toml` is the *source* of the variable bag; per-brick YAMLs and SQL
consume it via `${var.name}`. References resolve in this order (last wins):

1. `instances/<name>/foundry.toml` -- instance identity (`name`, `schema_prefix`, ...)
2. `[targets.<target>]` in the repo-root `foundry.toml` -- environment overrides (`host`, `catalog`, `profile`, ...)
3. `--vars key=value` -- CLI overrides (repeatable)
4. `deployer` -- auto-resolved from the SDK's `current_user.me()` if not set

Target overrides win over instance identity, and CLI `--vars` win over both.
`foundry.toml` fields are literal values only: a `${...}` token inside a
typed field is rejected at load time (it would create a circular template
language). Keep `${var.*}` content in the per-brick YAMLs.

## Variable syntax

Per-brick configuration files -- YAML and SQL -- use the `${var.name}` syntax:

```sql
-- SQL example
CREATE VIEW ${var.catalog}.${var.schema}.my_view AS ...
```

```yaml
# YAML example
index:
  name: "${var.catalog}.my_schema.my_index"
```

The shared substitution logic lives in `agent_foundry.variables` and is
imported by every brick script.

## CLI usage

```bash
# Pick the target on every CLI call
uv run agent-foundry deploy --target dev   --instance portfolio-assistant
uv run agent-foundry deploy --target staging --instance portfolio-assistant
uv run agent-foundry deploy --target prod  --instance portfolio-assistant

# Override individual variables on the command line
uv run agent-foundry deploy --target dev --instance portfolio-assistant \
    --vars catalog=experimental --vars warehouse_id=abc123
```

`--target` defaults to `dev` for every verb that accepts it.

## Unsupported layouts

`foundry.toml` is the only configuration source. Files from earlier layouts
are not ignored silently: `agent-foundry validate`, `deploy` and `destroy`
fail and list them.

| Leftover | Replacement |
|---|---|
| `conf/targets/<env>.yml` (`variables:` block) | `[targets.<env>]` in the repo-root `foundry.toml` |
| `instances/<name>/instance.yml` | `instances/<name>/foundry.toml`, listed under `[[instances]]` |
| `instances/<name>/agent_app/` | `instances/<name>/app/` |
| `spec.runtime.llm_endpoint_name` | `spec.runtime.model: {kind: serving_endpoint, name: ...}` |

Deploy state lives in the workspace only
(`/Shared/agent-foundry/<instance>/_state/`); local `_state/` directories are
no longer read and can be deleted.

Keep developer-local target settings (host, profile) out of version control,
or provide them in CI through environment variables (`DATABRICKS_HOST`,
`DATABRICKS_CONFIG_PROFILE`).
