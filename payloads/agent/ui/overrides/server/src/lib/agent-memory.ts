/**
 * Agent Bricks: long-term agent memory (`spec.runtime.context.memory`, #787), the pure half.
 *
 * Memory is a set of facts about the signed-in user, filed under short
 * topics, kept in the chat-history Lakebase (see `agent-memory-store.ts`).
 * The agent's `_remember`, `_recall`, `_forget` and `_list_memories` call
 * the routes in `routes/agent-memory.ts`.  This module parses their bodies
 * and ranks facts for `_recall` with the history search's term weights.
 * No I/O and only type imports, so it is unit-tested out of tree at
 * `payloads/agent/ui/tests/ts/`.
 */
import { match, normalize, queryTerms, termWeights } from './history-search';

export const DEFAULT_MAX_TOPIC_CHARS = 8_000;
export const MAX_RECALL_HITS = 10;
export const MAX_QUERY_CHARS = 500;
/** As the agent's `topic_path` makes them: lower-case letters, digits, `-` and `_`, at most 64 characters. */
const TOPIC_PATTERN = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;

export interface MemoryFact {
  topic: string;
  fact: string;
}

export interface TopicCount {
  topic: string;
  facts: number;
}

/** *text* with its whitespace collapsed, as the agent normalizes a fact. */
export function normalizeFact(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(' ');
}

/** What makes two facts of one topic the same: case and spacing are ignored. */
export function factKey(fact: string): string {
  return normalizeFact(fact).toLowerCase();
}

export function isTopic(value: unknown): value is string {
  return typeof value === 'string' && TOPIC_PATTERN.test(value);
}

/** The topic and normalized fact of *body*, or an error message. */
export function parseFactBody(body: unknown, maxChars: number): MemoryFact | { error: string } {
  const { topic, fact } = (body ?? {}) as { topic?: unknown; fact?: unknown };
  if (!isTopic(topic)) {
    return { error: 'topic must be 1 to 64 lower-case letters, digits, "-" or "_".' };
  }
  const normalized = typeof fact === 'string' ? normalizeFact(fact) : '';
  if (!normalized) {
    return { error: 'fact must be non-empty text.' };
  }
  if (normalized.length > maxChars) {
    return { error: `The fact is longer than ${maxChars} characters.` };
  }
  return { topic, fact: normalized };
}

/** The query of *body*, or `null` when it is missing, empty or too long. */
export function parseMemoryQuery(body: unknown): string | null {
  const query = (body as { query?: unknown } | null)?.query;
  if (typeof query !== 'string' || query.length > MAX_QUERY_CHARS || !query.trim()) {
    return null;
  }
  return query;
}

/** Characters a topic holds with these facts, one per line. */
export function topicChars(facts: readonly string[]): number {
  return facts.reduce((sum, fact) => sum + fact.length, 0) + Math.max(facts.length - 1, 0);
}

/** Up to {@link MAX_RECALL_HITS} of *facts* matching *query*, best first; the topic name counts too. */
export function searchFacts(facts: readonly MemoryFact[], query: string): MemoryFact[] {
  const phrase = normalize(query);
  const terms = queryTerms(phrase);
  if (terms.length === 0) {
    return [];
  }
  const lowered = facts.map((entry) => `${entry.topic.replace(/[-_]/g, ' ')}: ${entry.fact}`.toLowerCase());
  const weights = termWeights(lowered, terms);
  return facts
    .map((entry, order) => ({ entry, order, found: match(lowered[order], phrase, weights) }))
    .filter((hit) => hit.found !== null)
    .sort((a, b) => (b.found?.score ?? 0) - (a.found?.score ?? 0) || a.order - b.order)
    .slice(0, MAX_RECALL_HITS)
    .map((hit) => hit.entry);
}
