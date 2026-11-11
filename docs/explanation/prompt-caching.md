# Prompt caching in the agent payload

> **Explanation** — why the agent payload caches the way it does, and what the
> arrangement costs. For the knobs themselves see
> [Payload config](../reference/config.md#payload-config-payloadsagentpayloadyml);
> for the wider set of context-management techniques, see
> [Context management design](context-management-design.md).
> For the authoring-side trade-offs, the
> [portfolio-assistant payload README](../../examples/portfolio_assistant/payloads/agent/README.md)
> covers the same ground from a user's point of view.

## The problem this solves

Production traces of a deployed agent put **98% of all tokens on the input side**,
averaging 94,075 input tokens per request. Almost none of that is the user's
question. It is the same system prompt, the same tool definitions, the same skill
bodies and the same conversation history, re-sent on every call of the agent loop —
and the loop makes several calls per request, each one re-sending everything the
previous call sent plus its result.

Every context-management technique in the literature attacks that volume by
*removing* something: trim tool results, summarise history, load instructions on
demand. All of them trade accuracy for cost, because the model no longer sees what
it saw before. Prompt caching is the exception. The model sees byte-identical input;
only the price changes. That is why it went first, and why it is the only lever in
this area currently shipped.

## What the platform allows

The runtime calls Databricks model serving through
`set_default_openai_api("chat_completions")`, which rules out every server-side
primitive both vendors ship — Anthropic's `context_management` (tool-result
clearing, compaction, thinking clearing, the memory tool) and OpenAI's Responses-only
`responses.compact` are all unreachable from the OpenAI-compatible surface. Provider
prompt caching is reachable, because it travels as a `cache_control` field on
individual content blocks of an ordinary chat-completions request.

## The cost model

Four numbers govern every decision below, and the write price depends on the TTL:

| Event | Price, relative to a fresh input token |
|---|---:|
| Cache **write**, rolling 5-minute tier | 1.25x |
| Cache **write**, long 1-hour tier | 2.0x |
| Cache **read** (the prefix matched) | 0.1x |
| No caching | 1.0x |

A rolling write costs 25% more than sending the tokens fresh, so **a cached prefix pays
for itself the first time it is re-read**: two uncached requests bill 2.0x the prefix,
while a write followed by a read bills 1.35x. The crossover is one hit.

The long tier doubles the write, so it needs more than one hit to break even: a 1-hour
write followed by reads bills 2.0x + 0.1x per read, against 1.0x per uncached request.
Break-even lands at the second read. That is a fine trade for a static prefix an
instance re-reads on every request for an hour, and a bad one for anything volatile —
which is the whole argument for the ordering in the next section.

Because the tiers are priced differently, the modelled cost of a deployment is

```
billed = 0.1 x cache_read + 1.25 x rolling_write + 2.0 x long_write + 1.0 x uncached
```

The observability dashboard's **Token & Cache Economics** tab computes exactly this,
per request and per day. See [reading the dashboard](#reading-the-dashboard).

The consequence for low-traffic agents is worth stating plainly, because it is the
one case where caching loses: if requests arrive far enough apart that the prefix
goes cold every time, you pay 1.25x forever and never read. The TTL is a **sliding
window refreshed for free on every hit**, not a fixed lease — so the threshold is not
"one request per TTL period on average" but "no gap longer than the TTL". At
`static_ttl: "1h"`, an agent in continuous business-hours use never pays a second
write.

## Where the marker goes, and why there

Anthropic caches **cumulatively from the start of the prompt**: everything up to and
including a marked block is cached together, and a marker is a *prefix boundary*, not
a region. Two rules fall out of that, and they explain most of the design:

1. **Volatile content early in the prompt invalidates every breakpoint after it**,
   not just its own. So content must be ordered stable-to-volatile.
2. **The marked prefix must be byte-identical** across requests to be read at all.
   Not semantically equal — byte-identical.

The order the payload assembles, from stable to volatile:

```
tools → system prompt → skills → tool manuals → data universe
  ── breakpoint (1h) ──
identity (own message) → conversation history → the new question
  ── breakpoints (5m, two of them, walking back from the tail) ──
```

The long-TTL breakpoint covers what a deployment fixes and a request never changes.
The two rolling breakpoints pay off *within* a request rather than across requests:
the next call in the agent loop re-sends the history this call just wrote, including
its tool results, which are 76.3% of input volume.

Three breakpoints out of the provider's limit of four, leaving one spare.

## Why the injection point is a client subclass

The OpenAI Agents SDK serialises `Agent.instructions` into a bare
`{"role": "system", "content": <str>}`, and a string cannot carry a per-block marker.
There is no seam higher up. So `PromptCachedCompletions`
([`prompt_cache.py`](../../payloads/agent/runtime/prompt_cache.py)) wraps
`chat.completions.create`, rewrites the outgoing message list, and calls
`super().create()` **last** so that `databricks-openai`'s own payload fixes and
MLflow's tracing patch stay in the chain behind it.

That placement has a second benefit that turned out to matter more than the first.
The client is the only layer that can add a message to a request *without it being
persisted into conversation history* — which is what makes the identity arrangement
below possible.

## Identity: the failure mode that shaped the rest

The per-request identity (`inject_time_user`) is the smallest piece of content in the
prompt and caused the most trouble, because it is the only volatile thing that used
to sit in front of everything stable.

**Minute resolution to date resolution, and back.** A wall-clock stamp in front of
the history invalidates it sixty times an hour, so the identity was cut to the UTC
date. Since the identity moved behind the history (below), the time to the minute is
back at no cache cost.

**Never written into history.** An intermediate design stamped the newest user turn
instead of the system prompt. That is worse than it sounds: the stamp is applied to
the *newest* turn only, so on the next request the same message arrives unstamped, the
bytes differ from what was cached, and the cache is invalidated from that point on —
taking the previous turn's tool results with it. It capped savings at 59% and the
cause was invisible in the numbers. History is now never touched.

**Its own message, after the breakpoint.** Four shapes were measured against
`databricks-claude-sonnet-4-6` before choosing: a second content block, a second
`system` message, a `developer` message and a `user` message. All four are accepted,
all four read the identical cached prefix, and none re-writes a token when the
identity changes. Since cost was identical the choice was readability: a message
shows up in a trace as request context rather than a stray line trailing the system
prompt.

**Behind the history.** A message right after the system prompt is still in front of
the history, so any change there re-writes the whole history, not just one line. That
was cheap while it held only a per-user identity, and expensive once the memory index
and the plan, which change from turn to turn, joined it. The parts that change
between turns (identity, memory index, plan) now go out as the **last** message,
after the history; the per-user skill index and the unavailable-tools note stay in
the message after the system prompt. Measured on `databricks-claude-sonnet-5-5` with
an 18k-token prompt and only the context text changing: in front of the history,
each warm request read the 7.9k static prefix and re-wrote 10.2k of history; behind
it, each warm request read all 18.1k and wrote nothing. A trailing `system` and a
trailing `user` message cached the same; `system` was kept because the text is
framework context, not something the user said.

A related rule follows: rolling breakpoints skip system-role messages. A marker on
either extra message would spend a breakpoint on a prefix ending in content that
changes, so it would be re-written rather than read.

## Why caching forces eager skills

Lazy skill loading and prompt caching are mutually exclusive in the payload, and
caching wins with a deploy warning. The reason is not redundancy but cost: lazy skill
bodies arrive as **tool results**, which land in the conversation *behind* the
volatile tail, so they are re-written at full price on every request instead of riding
the cached prefix — and each activation costs an extra LLM round trip.

Measured on a multi-skill question, in billed units (a read counted at 0.1, a write at
1.25):

| Mode | Billed units | Saving vs uncached |
|---|---:|---:|
| eager, warm | **1,892** | 86.4% |
| lazy, warm | **5,685** | 59.1% |

Eager is 3.0x cheaper. The intent behind `lazy` — a smaller prompt — is better served
by a cached large prompt than an uncached small one. Lazy remains the right choice
only with caching off: a very large skill library, or a prompt approaching the
context window.

## What breaks the cache

Everything in the marked prefix must be byte-stable across requests, which makes a
few authoring mistakes expensive rather than merely untidy:

- **A clock anywhere in the prefix.** Including one rendered indirectly, e.g. a
  `_refreshed_at` column in the data universe function's projection — the renderer
  warns about column names that look time-varying for this reason.
- **Per-user content in the prompt.** Anything that differs between users belongs
  behind the breakpoint, or the prefix is per-user and the hit rate collapses with
  the number of users.
- **Non-deterministic rendering.** Dictionary iteration order, unsorted rows,
  anything that can produce the same content in different bytes. The data-universe
  renderer is deliberately pure and order-preserving for this reason.
- **An unrecognised TTL string.** The gateway accepts it and silently declines to
  cache, so the contract restricts TTLs to `"5m"` and `"1h"`.

The runtime logs a hash of the cacheable body on every request, so prefix instability
shows up as a changing hash rather than as an unexplained cost.

## What this is not

Caching reduces the *price* of context, not its size — so it does nothing for
**context rot**, the gradual accuracy decay Anthropic attributes to a finite
attention budget. A cheap 90,000-token prompt is still a 90,000-token prompt. That is
why the design doc keeps tool-result capping, compaction and tool-definition gating as
separate, later steps: they attack volume, they carry accuracy risk, and shipping any
two of these at once makes the cost delta impossible to attribute.

## Verifying it in production

The provider reports cache accounting on every LLM span. MLflow 3.16.1 copies
Databricks Claude's top-level `cache_read_input_tokens` / `cache_creation_input_tokens`
into `mlflow.chat.tokenUsage` (the trace Usage panel). Nested 1h vs 5m writes stay on
`mlflow.spanOutputs.usage.cache_creation` and are **not** in `tokenUsage`.

Chat uses `Runner.run_streamed`. openai-agents only sends `stream_options.include_usage`
for the official OpenAI client; the agent payload sets `ModelSettings(include_usage=True)`
so Databricks streams still return a usage chunk. Without that flag, autolog sees
`usage=None` and cache reads look like 0.

`mlflow.openai.autolog()` also registers an Agents SDK tracer that emits a second
`CHAT_MODEL` span named `Generation`. That span uses the requested serving-endpoint
name and has no Databricks cache extras. The HTTP span (`PromptCachedCompletions`)
has the gateway-resolved model and the cache fields. MLflow's cost UI then shows
two models and DFS-dedups `tokenUsage` onto the parent, which zeros cache reads.
Init disables the Agents SDK tracer so one LLM span remains. A span processor
stamps `mlflow.llm.provider` from the Databricks OpenAI client identity (not a
model-name map), which the cost UI otherwise leaves as Provider NULL on
`global.anthropic.*` names.

In the MLflow trace tables / Lakeview economics tab:

| Field | Where | Reading |
|---|---|---|
| `cache_read_input_tokens` | `spanOutputs.usage` extras, else `mlflow.chat.tokenUsage` | tokens billed at 0.1x — the win |
| `cache_creation_input_tokens` | same | total written this call, across both tiers |
| `cache_creation.ephemeral_1h_input_tokens` | `spanOutputs.usage` only | writes on the static tier at 2x; should be 0 on warm traffic |
| `cache_creation.ephemeral_5m_input_tokens` | `spanOutputs.usage` only | writes on the rolling tier at 1.25x; the new turn, expected every request |

A healthy warm request on the reference instance reads ~7,000 tokens at 0.1x, writes
0 on the 1h tier, and writes a few hundred on the 5m tier — roughly 1,150 billed
units against 7,373 uncached.

### Reading the dashboard

`prompt_tokens` — and therefore everything MLflow reports as input tokens, including
`mlflow.trace.tokenUsage` and the trace UI's own counters — is the **uncached-equivalent**
total. It counts tokens that were served from cache at full weight. A working cache
therefore changes none of those numbers, which makes the trace UI a poor place to look
for the saving and a reliable way to conclude, wrongly, that caching is not working.

The **Token & Cache Economics** tab exists for this. It reads cache **read** and
total **write** from `spanOutputs.usage` extras, falling back to
`mlflow.chat.tokenUsage`, and the 1h vs 5m write split from nested
`spanOutputs.usage.cache_creation`. It splits daily input volume into the four
priced buckets, so the shape of the stacked bar tells you what is happening at
a glance:

- mostly **cache read** — working as designed
- a **static write** on more than the occasional request — the prefix has stopped being
  byte-identical; check the section above on what breaks the cache
- mostly **rolling write** — the prefix is fine and the cost is new content, usually
  tool results. The *Tool Results by Weight* panel ranks the sources; caching cannot
  help with a token's first appearance, so that is a volume problem, not a cache one.
- mostly **uncached** — caching is off, the model is not a Claude endpoint, or the
  markers are being dropped

*Prefix rebuilds per 100 requests* is the compact version of the second bullet: a static
write should happen roughly once per hour per replica. If it tracks request count, the
prefix is unstable.

Billed units on the tab are **modelled** from the multipliers above rather than read from
billing. The DBU counter beside them comes from `system.billing.usage` and is the actual
charge; use the modelled figure to compare configurations and the DBU figure to reconcile
against an invoice.
