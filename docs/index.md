# Agent Foundry Documentation

The single map of the documentation tree. Start with the
[repository README](../README.md) for the fork → team-repo model, bricks, and
payloads. Docs below are organized by the
[Diátaxis](https://diataxis.fr/) taxonomy — every page is exactly one of:
**tutorial**, **how-to**, **reference**, or **explanation**.

> This index is the one doc map. A CI link checker
> (`tests/contracts/test_docs_links.py`) fails the build on any dead relative link, so
> this page cannot silently drift out of sync with the tree.

## Tutorials

Learning-oriented, end-to-end paths.

| Page | What it covers |
|------|----------------|
| [Getting started](tutorials/getting-started.md) | Init → configure → deploy → set up CI/CD, start to finish. |

## How-to guides

Task-oriented, cross-cutting recipes.

| Page | What it covers |
|------|----------------|
| [Integrate MCP & A2A](how-to/integrate-mcp-a2a.md) | Calling patterns for MCP/A2A providers and consumers. |
| [Deploy via CI/CD](how-to/deploy-cicd.md) | The three-stage pipeline, secrets, and approval gates for generated app repos. |
| [Fork for platform teams](how-to/fork-for-platform-teams.md) | How framework CI works and what to configure when you fork this repo. |
| [Author a brick](how-to/author-a-brick.md) | Add a new deployable brick (built-in or third-party). |
| [Author a payload](how-to/author-a-payload.md) | Add a new App payload adapter (built-in or third-party) and its scaffolds. |

## Reference

Information-oriented lookups. Pages under `reference/` are kept honest by
generators and freshness gates where the source is code.

| Page | What it covers |
|------|----------------|
| [CLI](reference/cli.md) | Every `agent-foundry` verb, with examples. |
| [Configuration](reference/config.md) | Target/instance config shape and `${var.name}` resolution. |
| [Environment variables](reference/env-vars.md) | Generated from `agent_foundry/env.py`. |
| [Bricks](reference/bricks.md) | Per-brick capability summary. |

## Explanation

Understanding-oriented background and design.

| Page | What it covers |
|------|----------------|
| [Architecture](explanation/architecture.md) | System design, deployment pipeline, data flow. |
| [Prompt caching](explanation/prompt-caching.md) | Why the agent payload caches the way it does, and what it costs. |
| [Context management design](explanation/context-management-design.md) | Context-engineering techniques, ranked, and which ones the payload implements. |
| [Conversation compaction](explanation/conversation-compaction.md) | How long chats continue past the context window: persisted summaries, triggers, and why not provider-side. |
| [Agent memory and plans](explanation/agent-memory.md) | The three context layers (history, per-chat plan, long-term memory), where each is stored in the chat-history Lakebase, bound identity, cleanup. |
| [Unity Gateway integration](explanation/unity-gateway-integration.md) | Live findings for model services, MCP services and skills through Unity Gateway. |
| [Speech-to-text input design](explanation/speech-to-text-design.md) | Mic button in the chat UI: one transcription contract, Whisper on a T4 endpoint built by the `model_serving` brick, Gemini and Custom LLM Serving as later backends. |

## Payloads

Application code that runs inside the App host. Each payload owns one README.

| Payload | Doc |
|---------|-----|
| `agent` | [README](../payloads/agent/README.md) |
| `rest_mcp` | [README](../payloads/rest_mcp/README.md) |

## Bricks

Each brick keeps **one** authored doc at `bricks/<key>/README.md` (the
when/why/gotchas a generator can't produce).

| Brick | Doc |
|-------|-----|
| `ai_gateway` | [README](../bricks/ai_gateway/README.md) |
| `app` | [README](../bricks/app/README.md) |
| `tracing` | [README](../bricks/tracing/README.md) |
| `data` | [README](../bricks/data/README.md) |
| `evaluation` | [README](../bricks/evaluation/README.md) |
| `genie` | [README](../bricks/genie/README.md) |
| `job` | [README](../bricks/job/README.md) |
| `mcp` | [README](../payloads/agent/deploy/mcp/README.md) |
| `model_serving` | [README](../bricks/model_serving/README.md) |
| `observability` | [README](../bricks/observability/README.md) |
| `skills` | [README](../payloads/agent/deploy/skills/README.md) |
| `sql_function` | [README](../bricks/sql_function/README.md) |
| `vector_search` | [README](../bricks/vector_search/README.md) |
| `warehouse` | [README](../bricks/warehouse/README.md) |
