# Agent memory and plans

> **Explanation** -- how the agent payload keeps state outside the context
> window, and why it is split the way it is. For the knobs see
> [Payload config](../reference/config.md) (`spec.runtime.context.plan`,
> `spec.runtime.context.memory`); for the wider token budget see [Conversation compaction](conversation-compaction.md)
> and [Prompt caching](prompt-caching.md).

## The problem

A long-running agent works across many model calls, turns and context resets.
The context window is working memory: it is finite, and long contexts degrade
as stale material piles up. Whatever the agent must not lose has to live
outside it and come back at the right moment. Two different things need that:

1. **The state of the current work** in one chat: the goal, what is done with
   its result, what is still open. It changes every few calls and is useless
   once the chat is gone.
2. **What stays true after the work is done**: the user's preferences and
   role, facts about their environment, lessons learned. It changes rarely and
   is useful in every future chat.

Treating both as one "memory" with a scope flag made the lines blurry: chat
notes for "decisions, progress or results" overlapped with the plan, its
history and the compaction summary, and the model rarely knew which to use.

## Design: three layers

The split follows the working-memory / long-term-memory distinction of
[CoALA](https://arxiv.org/abs/2309.02427) and the practice of long-running
harnesses: a progress file re-read on every restart, kept apart from durable
project knowledge (Anthropic's
[context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)
and [long-running harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
guidance; Claude Code's todo list versus `CLAUDE.md`).

| Layer | Holds | Owner | Lifetime |
|-------|-------|-------|----------|
| History | Messages, cleared tool results, the compaction summary | The framework | This chat, increasingly compressed |
| Plan | The goal and outcome milestones; finished goals | The agent (`_update_plan`) | This chat; deleted with it |
| Long-term memory | Facts that outlive the chat | The agent, when the user states or asks; later a consolidation step | Across the user's chats |

The test for where something belongs: *if this task were abandoned now, would
it still be useful in a new chat?* No: the plan (or the history). Yes:
long-term memory. "Storage sizes estimated" is plan; "the workspace id is
1234567890123456" and "`system.serving.served_entities` has no `entity_name`
column" are memory.

Each layer reaches the model the same way: a block in the cached prefix says
how to use it, and the per-request part (plan, memory index) goes into the
request context, the last message after the history, so it never invalidates
the cached history.

### History

Unchanged: [tool-result clearing](context-management-design.md) and
[compaction](conversation-compaction.md). The framework decides what is kept;
the agent writes nothing here. Both can be undone: a cleared result by calling
the tool again, the turns behind a summary with `_search_history` and `_read_history`,
which the agent has while compaction is on
([details](conversation-compaction.md#searching-the-summarized-turns)).

### Plan

With `spec.runtime.context.plan.enabled` the agent keeps a goal and a few
outcome milestones for long work in the chat, closer to the feature list of
Anthropic's long-running harness than to a step checklist like Codex's
`update_plan` or Claude Code's `TodoWrite`. Modern models do several actions
per model call and track the small steps themselves; a step list made them
spend calls on bookkeeping (Manus found a third of its actions went to
rewriting `todo.md`) and they ignored the one-step-in-progress rule. What steers
the model is not that the plan is stored but that it is recited at the end of
the context.

| Tool | Does |
|------|------|
| `_update_plan(goal?, add?, done?, drop?)` | Patches the plan: a goal starts one or changes its aim, `add` appends milestones, `done` marks milestones (by number) with a one-line result, `drop` with a reason. Several at once is fine; up to 10 milestones. Answers with the open milestones. |
| `_delete_plan()` | Deletes this chat's plan. |

- **Storage.** The plan is a row of `chat_plans` in the chat-history Lakebase,
  keyed by the chat id. `content` is the open plan, rendered as markdown
  (`**Goal:**`, then `1. [x] Inventory — 412 tables`,
  `2. [-] Lineage — no access`, `3. [ ] Costs`). When a new goal replaces a
  finished plan, the finished one is appended to `history`; the oldest plans
  are dropped once it passes `max_chars`. Both are saved in one write, and
  the row is deleted with the chat by a foreign-key cascade.
- **Recitation.** While a milestone is open, the plan goes last into the
  request context as `## Current plan`, rebuilt before every model call (the
  agent's instructions are a callable then), so a plan made or updated within
  one turn is in front of the model on its next call.
- **Finished goals.** Below it, `## Finished goals in this chat` lists the last
  five finished goals, one line each with their outcome (`5 done, 1 dropped`),
  including the current plan once nothing in it is open. That is how the agent
  sees what it already did in the chat after compaction, without searching
  the history.
- **Reminder.** After 8 model calls without an update, the recited plan adds
  one line asking to mark finished milestones or drop ones that no longer
  apply, like Claude Code's periodic todo reminder.
- **Guidance.** A `## Planning` block in the cached prefix: for work that takes
  many tool calls or turns, set a goal and 3 to 7 outcome milestones; decide
  routine choices and say so, but ask at real decision points (ambiguity that
  changes the result, trade-offs only the user can judge, costly or
  irreversible steps) with two to four options and a recommendation; update
  when milestones are done or dropped or the approach changes, not after every
  action; mark done only once the result is checked. With long-term memory on,
  it adds: anything that still matters after the goal is done goes to
  `_remember`.
- **Cost.** Every update is a tool call, which is why milestones are few and
  coarse and one call can mark several. The plan is read once per request
  and the tools keep the request's copy current, so reciting reads nothing.
  If that read fails, the plan tools answer "plan unavailable" instead of
  overwriting a plan the request never saw.
- **Chat only.** The plan is stored with a chat of the chat UI, so it requires
  `spec.ui.chat_history`; MCP and A2A get no plan tools. An `/invocations`
  caller whose `conversation_id` is not a chat it owns gets tools that answer
  "plan unavailable".

### Long-term memory

With `spec.runtime.context.memory.enabled` the agent keeps facts about the
signed-in user that outlive a chat. The four tools follow Databricks' reference
`databricks_agentkit.openai.memory`, without a scope: every fact is the user's.

| Tool | Does |
|------|------|
| `_remember(fact, topic)` | Files one normalized fact under a topic, shown as `/<topic>.md`; a fact already stored is a no-op. |
| `_recall(query)` | Searches the user's facts, grouped by topic. |
| `_forget(fact, topic)` | Removes one fact; the topic can be a path from the index. |
| `_list_memories()` | Lists topics with their fact count. |

- **Guidance.** A `## Using memory` block in the cached prefix: save stable
  facts about the user and their environment when the user states them or asks;
  check memory before asking for something the user may have said before; never
  save secrets.
- **Index.** Each request lists the user's topics (path and fact count; at most
  `index_max_entries`) in the request context, so the model knows what exists
  without a tool call. Fact text is not in the index; `_recall` reads it.
- **Paths.** Memory needs no conversation, so `/invocations` callers without
  one get it too, as long as the request carries a signed-in user.
- **Not working notes.** Nothing chat-specific is written here; the plan and
  the history own that.

### Where each layer is stored

The plan belongs to a chat, so it lives with the chat in the chat-history
Lakebase. The agent server opens no database connection: it reads and saves
the plan through `/api/chat/:id/plan` on the chat UI server over localhost,
like the history search. The chat UI server owns the table and accesses it as
the App's service principal, like the rest of the chat history.

Long-term memory lives there too, keyed by user instead of chat. Each fact is
a row of `agent_memory_facts` (`user_id`, `topic`, `fact`, a case- and
spacing-insensitive `fact_key`, `created_at`), created on first use. The
agent's memory tools call `/api/agent/memory/*` on the chat UI server, which
scopes every query to the session's user. `_recall` is a lexical search over
the user's facts and topic names, with the same term weighting as
`_search_history`. One topic listing per request builds the memory index,
a single indexed query against the database the chat already uses.

An earlier version kept memory in a Databricks managed memory store (Beta).
Its listing took about 13 seconds before the first token, so it was dropped
for the Lakebase the payload already runs.

## Identity and access

- **The actor is bound, never a tool argument.** It is the `x-forwarded-user`
  id the Databricks Apps proxy sets on every request; the chat UI server
  forwards it to the agent server. It is the same value as the chat's `userId`,
  which is how the plan route checks that the caller owns the chat. The model
  cannot name another actor: unknown tool arguments are rejected.
- **Users get no database access.** The agent forwards only the identity
  headers to the plan and memory routes, no token or cookie. The plan route
  answers 403 to anyone but the chat's owner; the memory routes read and write
  only the session user's rows. Both run their queries as the App's service
  principal.
- **The proxy overwrites a forged header.** The smoke probe
  `invocations memory across chats` saves under a forged `x-forwarded-user`
  and recalls without it; it only passes when both land under the caller's
  real identity.
- **A user can reach only their own memory.** The memory routes are behind
  the same session as the rest of the chat UI, so a signed-in user could call
  them from the browser, but only for their own facts.

## Lifecycle

| Event | Effect |
|-------|--------|
| `deploy` | Nothing for memory or the plan: their tables are created on first use. |
| Chat deleted | The chat's plan row goes with it (foreign-key cascade). |
| Chat expires (`retention_days`) | The retention sweep deletes the chat and, by the same cascade, its plan. |
| `destroy` | `destroy --force` deletes the Lakebase project, and with it every user's memory. |

Long-term memory is kept until the user asks the agent to forget it; chat
deletion and retention do not touch it.

## Next: consolidation

Agents rarely call a memory tool on their own initiative
([MemCog](https://arxiv.org/html/2605.28046) calls this the invocation
bottleneck; [Mem2ActBench](https://arxiv.org/html/2601.19935) finds memory
frameworks recall well when asked but fail to apply memory unprompted). The
systems that work over long horizons write memory at fixed points instead:
Letta's [sleep-time agents](https://www.letta.com/blog/sleep-time-compute/)
consolidate memory between turns, and
[Generative Agents](https://arxiv.org/abs/2304.03442) periodically turn
observations into lasting reflections.

The three layers leave a clear place for that: a background step, at
compaction or when a plan finishes, that promotes facts passing the test above
from history and plan into long-term memory. It is not built yet; until then
long-term memory is written only when the user states or asks for something.

## Limits and trade-offs

- **Lexical recall.** `_recall` matches words, not meaning; "coffee" does not
  find "espresso". The index lists every topic, so the model can also read a
  topic it expects to be relevant.
- **Memory needs chat history.** It is kept in the chat-history Lakebase, so
  `spec.runtime.context.memory.enabled` requires `spec.ui.chat_history.enabled`.
- **Facts in the old managed memory store are not migrated.** Deployments
  that used it start with an empty memory; the store is no longer created or
  deleted by deploy.
- **Without a forwarded user, neither the plan nor the memory tools are
  registered**, as in local development without the chat UI.
- **Agent sessions are out of scope.** Databricks' managed sessions store a
  transcript; the chat UI already keeps one in Lakebase.
