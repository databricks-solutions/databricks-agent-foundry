# portfolio-assistant — agent payload

This directory is the payload: everything the deployed agent *is*, as opposed to the
infrastructure it runs on (that lives in `../../app/config.yml`). Three things sit
here, and only the first is a config file:

| Path | What it is |
|------|-----------|
| [`payload.yml`](payload.yml) | The single config contract — model, prompt, limits, tool sourcing, context, UI, integrations |
| [`skills/`](skills/) | What the agent knows how to do. Four skills here: `catalog-explorer`, `sql-runner`, `documentation-explorer`, `chart-renderer` |
| [`mcp_servers/`](mcp_servers/) | Where its tools come from, with a per-server allowlist. Four servers here: Genie, Vector Search, UC functions, SQL warehouse |

The two directories are **presence-based**: they are active because they exist, and
carry no `enabled` toggle. Only *modes* live in `payload.yml`.

For the field-by-field reference see
[Configuration](../../../../docs/reference/config.md); for the reasoning behind the
caching design, [Prompt caching](../../../../docs/explanation/prompt-caching.md).

## What this example turns on

| Capability | Here | Default | Why this setting |
|---|---|---|---|
| `model` | `databricks-claude-sonnet-5-5` | `claude-sonnet-4-5` | Prompt caching needs a Claude endpoint; this one thinks |
| `reasoning` | `thinking: adaptive`, `effort: high`, `display: summarized`, `emit: true`; `max_tokens: 16000` | unset (endpoint default) | Thinks when it helps (Sonnet 5.5 rejects `enabled`); the chat UI shows the provider summary |
| `tools.mcp_only` | (default) `true` | `true` | Every tool call passes the MCP allowlist gate |
| `tools.skills.loading` | `eager` | `eager` | Cheaper than `lazy` whenever caching is on — see below |
| `context.prompt_cache` | on, `static_ttl: 1h` | on, `1h` | ~85% off the input bill on warm traffic |
| `context.tool_result_clearing` | on, keep `3`, clear ≥`300K` chars or above `100K` tokens | same | Bounds long conversations without touching Lakebase; the token trigger is the safety valve |
| `context.data_universe` | on, `refresh_minutes: 60` | **off** | Removes the "what data exists?" round trip |
| `inject_time_user` | (default) `true` | `true` | Agent knows who is asking and what time it is |
| `ui.chat_history` | on | on | Lakebase-backed history across sessions |
| `ui.file_upload` | **off** | off | Untrusted-input parsers; opt in only if needed |
| `ui.charts`, `ui.feedback` | on | on | `chart-renderer` needs charts on |
| `ui.tool_output.expanded` | (default) `false` | `false` | Wide SQL results would bury the answer behind raw panels |
| `integrations.a2a` | on, with card | off | Other agents can discover and call this one |
| `integrations.mcp_server` | on | off | Other systems can consume this agent as a tool |

## The model and the loop

`model` takes any Foundation Model API serving endpoint, but two things
have to agree with it: the same endpoint must be listed in `../../app/config.yml`
under `app_resources` (or the app's service principal cannot call it), and prompt
caching only applies to Claude endpoints — point this at a non-Claude model and the
cache markers are silently skipped rather than rejected.

`system_prompt` is the agent-level instruction. It ends up at the *top* of the
assembled prompt, with skill bodies and the data universe appended below it, so it is
the right place for cross-cutting response rules and the wrong place for anything
skill-specific.

Two limits guard the agent loop, and they are not equally real:

- **`max_iterations`** (10 here) is enforced. It caps LLM turns per request; hitting
  the cap ends the request as an error rather than returning a partial answer, so treat
  it as a runaway guard and not as a budget — set it above what your longest legitimate
  task needs.
- **`max_tool_calls_per_turn`** (5 here) is currently **advisory**. It is loaded into
  the runtime config but no code path enforces it yet. Set it to express intent, but
  do not rely on it to bound cost — `max_iterations` is what actually stops a loop.

## Where tools come from

With `mcp_only: true` (the default), the runtime attaches tools **exclusively** through
the MCP servers in `mcp_servers/`, and every server declares an allowlist that is
fail-closed: a tool not matched by an `allow` entry cannot be called, whatever the
model decides to try. That gives one auditable chokepoint for the agent's entire
capability surface. Setting `mcp_only: false` re-enables the direct Genie / Vector
Search / UC-function tool factories, which bypass that gate — worth knowing before
flipping it.

Skills do not provide tools; they provide *instructions for using* them. A skill's
body explains when to reach for a tool, what arguments matter, and how to present the
result. That split is why the allowlist can stay narrow while the skills stay verbose.

## Skill loading: eager or lazy

| | `eager` (this example) | `lazy` |
|---|---|---|
| Skill instructions | all inlined in the prompt | fetched via a `_load_skill` tool call |
| Prompt size | larger | smaller |
| Round trips | one per user turn | one extra per skill activated |
| Cost **with caching on** | baseline | **3x more expensive** |
| Cost with caching off | larger prompt, every request | smaller prompt, plus the extra calls |

**The counter-intuitive part.** With caching on, the smaller prompt is the expensive
one. Lazily loaded skill bodies arrive as *tool results*, which land in the
conversation behind the cache boundary — so they are re-sent and re-written at full
price on every single request, while eager skills ride the cached prefix at a tenth of
the price. Measured on a question needing three skills: 1,892 billed units eager
against 5,685 lazy.

Because of that, **the deploy overrides the combination**: authoring `loading: lazy`
alongside `prompt_cache.enabled: true` prints a warning and assembles eager anyway. To
genuinely run lazy, turn caching off.

**When lazy is still right.** Caching off, and either a large skill library (dozens of
domains, where inlining everything would crowd the context window) or a prompt already
close to the model's limit. With a handful of skills, eager plus caching is both
cheaper and fewer round trips.

## Prompt caching

**What you get.** The provider bills the stable head of each request — tool
definitions, system prompt, skills, the data universe — at **a tenth** of the normal
input-token price. On this example that is roughly 7,000 of 7,400 input tokens per
request, so a warm request costs about **15%** of an uncached one. Nothing about the
model's input changes; only the price does.

**What it costs.** Writing the cache costs **25% more** than sending those tokens
fresh. You pay that once when the prefix goes cold, and it is repaid the first time a
request re-reads it.

**When it is not worth it.** If your agent is quiet enough that gaps between requests
exceed the TTL, every request pays the 25% premium and never collects the discount.
The TTL is a sliding window refreshed free on every hit, so the question is not "how
many requests per hour" but "is there ever a gap longer than an hour". A demo agent
used twice a week is the case to switch it off; an agent in business-hours use is not.

**What you give up.** Two authoring freedoms, both usually fine:

- **No clock in the prompt.** A cache hit needs byte-identical input, so the injected
  identity carries the UTC *date*, not the time. If your agent needs the current time,
  give it a tool — the answer is then fresh when used instead of stale from
  prompt-assembly time.
- **No per-user content in the prompt.** Anything that differs per user has to sit
  behind the cache boundary, where the framework already puts the identity line. A
  prompt that varies per user gets a cache per user, and the hit rate falls with your
  user count.

```yaml
context:
  prompt_cache:
    enabled: true
    static_ttl: "1h"      # "5m" or "1h" only
```

## Data universe

**What you get.** The agent knows what data exists without spending a tool call to
find out — no "let me list the schemas" round trip before the actual work. The block
lists every queryable view with its location, row count and description, read from the
`get_data_landscape()` Unity Catalog function that
[`../../sql_functions/data_universe/`](../../sql_functions/data_universe/) deploys and
the [`data_universe_profile`](../../jobs/data_universe_profile/) job keeps populated.

**What it costs.** It rides in the cached prefix, so carrying a multi-thousand-token
catalog listing on every request costs roughly a tenth of what fetching it once per
conversation did. Turning caching off changes that arithmetic: then you pay full price
for the block on every request, and a tool-based lookup may be cheaper if your users
rarely need it.

**What to watch when authoring the function.** The rows must be stable between
requests, because the cache is keyed on bytes. A `refreshed_at` column in the
projection would re-write the whole prefix on every profiling run — the framework
warns about column names that look time-varying for exactly that reason. Row counts
changing when the data changes is fine and expected: that is a write you want, once
per change rather than once per request.

**Two consequences worth knowing.** The block is read as the **app's service
principal**, not as the signed-in user, because it must be identical for everyone to
be cacheable at all — so the function must only expose metadata every user of the app
may see. And the SQL stays adjustable without redeploying the app: the runtime only
ever runs `SELECT * FROM get_data_landscape()` and renders whatever columns come back,
so changing the projection or the descriptions is a `CREATE OR REPLACE FUNCTION`
picked up within `refresh_minutes`.

## The UI surface

`ui.enabled: false` makes the payload headless — the API is served, the bundled chat UI
is not. Useful when the agent is consumed only over A2A/MCP or by your own front end.
With the UI on, four features toggle independently:

| Toggle | On here | What it adds, and what to weigh |
|---|---|---|
| `chat_history` | yes | Persistent history in Lakebase, provisioned by the `app` brick. Off means every page load starts a fresh conversation |
| `file_upload` | no | Mounts the upload route *and* launches an isolated extractor process with its own parser dependencies. Default-off deliberately: those parsers face untrusted input, so enable it only if uploads are part of the job |
| `charts` | yes | Renders `echarts-chart` specs inline. The `chart-renderer` skill is pointless without it |
| `feedback` | yes | Thumbs up/down written to the trace, which is what makes production evaluation possible later |

`tool_output` sits alongside them but is a presentation *mode* rather than a mount
toggle — the tool-call panels are always there, and `expanded` only decides whether
each one starts open. It defaults to `false` because a turn that makes several tool
calls otherwise pushes the answer below a stack of parameter and result dumps. The
panel headers stay clickable either way, so set `expanded: true` when auditing every
call inline matters more than a tidy transcript.

## Integrations

Both are off by default and on here, because this example doubles as a reference for
agent-to-agent wiring.

- **`a2a`** mounts the A2A JSON-RPC routes and serves an agent card at
  `/api/.well-known/agent-card.json`. The `card` block is content you author — name,
  description, and the skills you advertise. Note that card skills are a *public
  contract for callers* and are deliberately coarser than the internal `skills/`
  directory: two card entries here cover four internal skills.
- **`mcp_server`** mounts `/mcp`, exposing this agent as a tool other systems can
  call. This is the exposure half of MCP; the consumption half (this agent using
  external MCP servers) is the `mcp_servers/` directory and has no toggle.

## Checking that it works

Ask the agent something, then look at the LLM span in the MLflow trace:

| What you see | What it means |
|---|---|
| `cache_read_input_tokens` large, `cache_creation_input_tokens` ~0 | working as intended |
| `cache_creation.ephemeral_1h_input_tokens` non-zero on every request | the prefix is changing between requests — something volatile got in front of the boundary |
| `cache_read_input_tokens` always 0 | prefix never re-read: traffic too sparse for the TTL, or a per-user prompt |

The system message should be one large block carrying the cache marker, with the
`[Context: ... | User: ...]` line in a **separate** message at the very end, after the
history. If you see that line inside the marked block or before the history, the
cache is being invalidated on every turn.
