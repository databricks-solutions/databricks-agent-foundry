---
name: documentation-explorer
description: >-
  Retrieve and synthesize Databricks documentation through semantic search. Use when the user asks what something is, how a platform feature works, or for best practices on topics such as ingestion and governance.
---

#### When to activate

Conceptual or knowledge questions about Databricks platform topics. Not for catalog structure queries — use catalog-explorer instead.

#### docs_search corpus

Each document: `{id, content, source, category}`.

| Column | Filterable | Notes |
|--------|------------|-------|
| `content` | **no** | Free-text body — never filter on this |
| `source` | LIKE | Document identifier (e.g. `"unity-catalog-overview"`) |
| `category` | exact | Closed set: `governance`, `storage`, `ml`, `ingestion` |

Filter syntax (only on filterable columns):

| Syntax | Example |
|--------|---------|
| `{"col": "val"}` | `{"category": "governance"}` |
| `{"col": ["a","b"]}` | `{"category": ["storage","ml"]}` |
| `{"col NOT": "val"}` | `{"category NOT": "ingestion"}` |
| `{"col LIKE": "token"}` | `{"source LIKE": "unity"}` |

When in doubt, search without filters first, then inspect returned values before narrowing.

#### Workflow

1. Extract the core concept from the user's question (e.g., "Unity Catalog governance" not the full sentence).
2. Call `docs_search__docs_index` with the concept as query. Add `filters` only when confident about column values.
3. Synthesize across results — combine information from multiple documents into a coherent answer.

#### Tool call examples

```
docs_search__docs_index(query="Unity Catalog governance")
→ [{"source": "unity-catalog-overview", "category": "governance", ...}]

docs_search__docs_index(query="ingestion best practices", filters={"category": "ingestion"})
→ filtered to ingestion docs only
```

#### Output format

- Lead with a concise answer, then supporting detail
- Cite sources when possible (e.g., "According to the Unity Catalog overview...")
- Synthesize, never dump raw document content

#### Edge cases

- **No docs:** "I couldn't find documentation on that topic. Try rephrasing."
- **Low relevance:** Acknowledge uncertainty: "The closest documentation covers X, which may be related."
- **Ambiguous topic:** Ask for clarification.
- **Needs catalog data too:** Suggest using catalog-explorer.
