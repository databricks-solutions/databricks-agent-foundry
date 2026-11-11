# Context management for the agent payload

> **Explanation** -- the industry practice for managing an agent's context
> window, ranked by impact on **accuracy** and on **token consumption**, and
> which of those techniques the agent payload implements. Researched Jul 2026
> against Anthropic and OpenAI primary sources (linked at the bottom). For the
> runtime this describes see [Architecture](architecture.md).

## The problem

An agent's context grows without bound unless something manages it: the UI
replays the chat transcript each turn, every MCP tool schema is listed from turn
one, and tool results enter context verbatim. The cost is paid on every model
call, and so is the accuracy loss described below.

## The principle both vendors converge on

Find the smallest set of high-signal tokens that still produces the outcome.
Accuracy degrades *gradually* as context grows — Anthropic calls the effect
**context rot** and attributes it to a finite attention budget, not to a hard
window limit. The practical consequence: a request that fits in the window can
still be measurably worse than the same request with half the tokens. Context
is a budget to spend, not a capacity to fill.

## What the platform takes off the table

[`runtime_config.py`](../../payloads/agent/runtime/runtime_config.py) calls
`set_default_openai_api("chat_completions")` against Databricks model serving.
That rules out every server-side primitive both vendors ship:

| Primitive | Vendor path | Why it is unavailable to us |
|---|---|---|
| Tool-result clearing (`clear_tool_uses_20250919`) | Anthropic `context_management` + beta header | Claude Developer Platform parameter; not exposed through the Databricks OpenAI-compatible surface |
| Server-side compaction (`compact_20260112`) | Anthropic `context_management` | Same |
| Thinking clearing (`clear_thinking_20251015`) | Anthropic `context_management` | Same |
| Memory tool (`memory_20250818`) | Anthropic tool type | Same |
| `responses.compact` / `OpenAIResponsesCompactionSession` | OpenAI Responses API | Responses-only, and OpenAI model names only — we are on `chat_completions` |
| `context_management=[{"type": "compaction", ...}]` | OpenAI Responses API | Same |

Conversation compaction is client-side regardless, see
[Conversation compaction](conversation-compaction.md).

**Every lever we adopt has to be client-side.** The usable hook is
`RunConfig.call_model_input_filter`, which *does* exist in the `[runtime]`
`openai-agents` line (`uv.lock` / `agent_foundry/constraints/runtime.txt`) and runs
immediately before each model call with the full rendered message list. Because
[`agent.py`](../../payloads/agent/runtime/agent.py) passes history in per
request rather than using an SDK `Session`, that filter — not a `Session`
subclass — is the natural insertion point for windowing, trimming, and
clearing. `SessionSettings(limit=N)` applies to Session-backed history, so it
does not help us as-is. `ToolOutputTrimmer` is not assumed to exist in the
pinned SDK line; that pattern is ours to write.

## Where the tokens actually go

Before choosing a compression strategy, note *which* category dominates.
Anthropic instrumented a research agent with no context management and bucketed
its context at the end of the run:

| Category | Tokens | Share |
|---|---:|---:|
| File-read tool results | 322,946 | 96.3% |
| Tool-call records | 6,287 | 1.9% |
| Agent reasoning text | 5,660 | 1.7% |
| User / task prompts | 357 | 0.1% |

The intuitive move — summarizing conversation history — targets almost nothing.
The levers that matter attack tool payloads: shape what tools return, clear
stale results, and only then compact. This is also the ordering Zylos'
production write-up recommends ("treat tool output as context waste by
default"; prevention outperforms reactive compression).

## The techniques, ranked

Ordered by impact on **accuracy** first, **token consumption** second. For
what the payload ships, see [What this payload implements](#what-this-payload-implements).

> This is the **generic** ranking from the literature. Measure a deployment's
> own token mix before applying it.

| # | Technique | Accuracy | Tokens |
|---|---|---|---|
| 1 | Tool-definition gating (deferred loading + tool search) | Very high | Very high |
| 2 | Sub-agent context isolation | Very high | High (main window) |
| 3 | Tool-result shaping at the source | High | High |
| 4 | Tool-result clearing / trimming in-window | High | Very high |
| 5 | Compaction (summarize, then re-initialize) | High | Very high |
| 6 | Just-in-time retrieval (references, not payloads) | High | High |
| 7 | Structured note-taking / external memory | High | Medium |
| 8 | Instruction pruning ("right altitude" system prompt) | Medium | Medium |
| 9 | History windowing (keep last N turns) | Medium | High |
| 10 | Context instrumentation (token breakdown per call) | Indirect | Neutral |
| 11 | Prompt-cache-stable prefix ordering | Neutral | High |
| 12 | Programmatic tool calling (code as context firewall) | Medium | High |

### 1. Tool-definition gating — the only lever with a published accuracy gain

Instead of loading every tool schema upfront, mark optional tools
`defer_loading: true` and give the model a search tool to discover them. The
model sees the search tool (~500 tokens) plus a few non-deferred critical
tools; matching definitions are expanded on demand.

- **Tokens:** a typical multi-server MCP setup burns ~55K tokens in definitions
  before any work happens. Anthropic's measured case went 77K → 8.7K (~85%
  reduction, 95% of the window preserved).
- **Accuracy:** internal MCP evals improved from **49% → 74%** (Opus 4) and
  **79.5% → 88.1%** (Opus 4.5). No other technique in this list has a published
  double-digit accuracy delta.
- **Anthropic's own trigger conditions:** ≥10 tools, or >10K tokens of
  definitions, or accuracy dropping as the toolset grows. Worth measuring where
  we sit before assuming.

### 2. Sub-agent context isolation

A lead agent holds the plan; sub-agents do deep work in their own clean windows
and return a condensed summary (Anthropic quotes 1,000–2,000 tokens per
sub-agent, against tens of thousands consumed internally).

- **Accuracy:** Anthropic's multi-agent research system reported a **90.2%
  improvement** over single-agent Opus 4 on their research eval — the largest
  single delta in the set.
- **Tokens:** the main window shrinks, but *aggregate* spend rises
  substantially, since each sub-agent re-pays for its own context. This is an
  accuracy-for-cost trade, not a saving.

### 3. Tool-result shaping at the source — cheapest real win

Make tools return the minimum actionable payload rather than compressing the
mess afterwards. File reads return relevant sections, API responses get
post-processed to key fields.

### 4. Tool-result clearing / trimming

Walk the message list and replace old `tool_result` payloads with a short
placeholder, keeping the `tool_use` record so the model still knows the call
happened. Safe because most tool output is re-fetchable.

- **Tokens:** Anthropic's measured run took the rendered message list from
  **128,740 → 43,060 tokens (−67%)** and held peak context at 173K where the
  baseline climbed to 335K.
- **Cost:** if the agent needs cleared data it re-calls the tool, so the
  clearing run can show *more* tool calls than the baseline. Cheap for a local
  file read, expensive for a rate-limited API. Tuning `keep` and `trigger`
  decides where you land.
- **Vendor defaults worth copying:** trigger at 100K input tokens, keep the last
  3 tool uses, plus `clear_at_least` so clearing only fires when it recovers
  enough tokens to justify breaking the prompt cache, and `exclude_tools` for
  results that must survive.

### 5. Compaction

Summarize a conversation approaching the limit and restart the window from the
summary plus the most recent artifacts.

- **Tokens:** peak held at 169K vs a 335K baseline in Anthropic's run.
- **Accuracy:** good for long-horizon coherence, but **lossy in a way you
  cannot fully tune away** — in the cookbook's own probe, a specific appendix
  figure the agent had read did not survive the summary. Anthropic's advice:
  tune the compaction prompt on real traces, maximize recall first, then trim
  for precision; preserve identifiers, paths, and decisions verbatim.

### 6. Just-in-time retrieval

Keep lightweight references (paths, queries, IDs) in context and load payloads
on demand, rather than pre-loading a corpus. Over-retrieval causes context
confusion as reliably as under-retrieval causes gaps.

### 7. Structured note-taking / external memory

The agent writes notes outside the window and pulls them back later. This is
what makes #4 and #5 safe: notes are what survives a reset. Anthropic ships
this as a file-based memory tool; OpenAI's cookbook treats a durable memo as the
memory primitive.

### 8–11. The cheap remainder

- **Instruction pruning.** Bloated always-loaded instructions cause the model to
  ignore the actual instructions. Relevant to us because eager skill mode
  concatenates every skill body into the system prompt
  ([`assemble.py`](../../payloads/agent/deploy/skills/assemble.py)); every rule
  in there is paid on every turn whether or not it is relevant.
- **History windowing.** Keep the last N turns. Cheapest bound on growth and a
  reasonable safety net, but naive cuts can drop the goal statement itself, so
  it is a fallback rather than a strategy.
- **Instrumentation.** No accuracy or token win by itself, but it is the
  prerequisite for choosing among the rest — "audit context composition before
  optimizing."
- **Prompt-cache-stable prefixes.** Pure cost, no accuracy effect: keep the
  system prompt and tool definitions byte-identical across calls so the provider
  can discount the cached prefix. Anything per-request, such as a timestamp or
  the user's identity, belongs after the cached prefix.

### 12. Programmatic tool calling — noted, not available

The model writes code that calls tools in a sandbox; intermediate results stay
in the execution environment and only the final value returns to context
(~37% token reduction reported). We have no sandbox in the payload, so this is
context for later rather than an option now.

## What this payload implements

| Technique | In the payload | Read more |
|---|---|---|
| 3. Tool-result shaping at the source | Compact SQL results with row and size caps, plus `_read_result` for the rest | [`sql_result_compact.py`](../../payloads/agent/runtime/sql_result_compact.py) |
| 4. Tool-result clearing | `context.tool_result_clearing`, a `call_model_input_filter` that keeps call/result pairs intact | [Payload config](../reference/config.md) |
| 5. Compaction | Client-side summarizer in the chat server, persisted in Lakebase | [Conversation compaction](conversation-compaction.md) |
| 7. Structured notes / memory | Per-chat plan and long-term agent memory in Lakebase | [Agent memory and plans](agent-memory.md) |
| 10. Instrumentation | Token & Cache Economics tab on the observability brick | [Observability brick](../../bricks/observability/README.md) |
| 11. Prompt-cache-stable prefix | `context.prompt_cache` breakpoints with eager skills | [Prompt caching](prompt-caching.md) |

## Sources

- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic cookbook — Memory, compaction, and tool clearing](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) (all measured trajectories above)
- [Anthropic — Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) (tool search, programmatic tool calling, tool use examples)
- [Anthropic docs — Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Anthropic docs — Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [OpenAI cookbook — Short-term memory management with Sessions](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory) (trimming and summarizing session patterns)
- [OpenAI cookbook — Building reliable agents with memory and compaction](https://developers.openai.com/cookbook/examples/agents_sdk/building_reliable_agents_memory_compaction)
- [OpenAI Agents SDK — Sessions](https://openai.github.io/openai-agents-python/sessions/) and [Model settings](https://openai.github.io/openai-agents-python/ref/model_settings/) (prompt cache options)
- [Zylos — Context engineering as a runtime discipline](https://zylos.ai/research/2026-04-19-context-engineering-agent-runtime-discipline/) (production guidance, audit-first)
