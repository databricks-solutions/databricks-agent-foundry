/**
 * Agent Bricks: search a chat's summarized turns (#685), the pure half.
 *
 * After a compaction the model sees a summary in place of the turns before
 * the boundary; `_search_history` lets the agent find exact details there
 * again.  The input is `summarizedTurns` (see `compaction.ts`); each text
 * or tool call in them is matched case-insensitively on the query's terms,
 * each weighted by how few passages contain it, so an id or an email counts
 * more than a common word.  A hit gives its turn, its offset in the turn's
 * text (where `history-read.ts` pages from) and an excerpt centred on the
 * best match.  No I/O and only type imports, so it is unit-tested out of
 * tree at `payloads/agent/ui/tests/ts/`.
 */
import type { TranscriptTurn } from './compaction';

export const MAX_HITS = 5;
export const EXCERPT_CHARS = 600;
const MIN_TERM_CHARS = 2;

export interface HistoryHit {
  /** 1-based user turn the hit belongs to. */
  turn: number;
  /** Offset of the match in the turn's text. */
  at: number;
  excerpt: string;
}

interface Passage {
  turn: number;
  start: number;
  text: string;
}

export interface Match {
  score: number;
  matchAt: number;
}

interface ScoredPassage extends Match {
  passage: Passage;
  order: number;
}

function passagesOf(turns: TranscriptTurn[]): Passage[] {
  return turns.flatMap((turn) =>
    turn.passages.map((span) => ({ turn: turn.turn, start: span.start, text: turn.text.slice(span.start, span.end) })),
  );
}

export function normalize(query: string): string {
  return query.toLowerCase().trim().replace(/\s+/g, ' ');
}

export function queryTerms(phrase: string): string[] {
  return [...new Set(phrase.split(' ').filter((term) => term.length >= MIN_TERM_CHARS))];
}

/** Inverse passage frequency of each term: rarer terms weigh more. */
export function termWeights(lowered: string[], terms: string[]): Map<string, number> {
  return new Map(
    terms.map((term) => {
      const containing = lowered.filter((text) => text.includes(term)).length;
      return [term, Math.log(1 + lowered.length / Math.max(containing, 1))];
    }),
  );
}

/** The weights of the terms found, plus all of them when the whole phrase occurs; `null` when none is found. */
export function match(lower: string, phrase: string, weights: Map<string, number>): Match | null {
  const found = [...weights]
    .map(([term, weight]) => ({ at: lower.indexOf(term), weight }))
    .filter((hit) => hit.at >= 0);
  if (found.length === 0) {
    return null;
  }
  const phraseAt = weights.size > 1 ? lower.indexOf(phrase) : -1;
  const termScore = found.reduce((sum, hit) => sum + hit.weight, 0);
  const phraseBonus = phraseAt >= 0 ? [...weights.values()].reduce((sum, weight) => sum + weight, 0) : 0;
  const best = found.reduce((top, hit) => (hit.weight > top.weight ? hit : top));
  return { score: termScore + phraseBonus, matchAt: phraseAt >= 0 ? phraseAt : best.at };
}

function excerptAround(text: string, matchAt: number): string {
  const start = Math.max(0, Math.min(matchAt - EXCERPT_CHARS / 2, text.length - EXCERPT_CHARS));
  const end = Math.min(text.length, start + EXCERPT_CHARS);
  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`;
}

/** Up to {@link MAX_HITS} excerpts of *turns* matching *query*, best first, newest first on ties. */
export function searchHistory(turns: TranscriptTurn[], query: string): HistoryHit[] {
  const phrase = normalize(query);
  const terms = queryTerms(phrase);
  if (terms.length === 0) {
    return [];
  }
  const passages = passagesOf(turns);
  const lowered = passages.map((passage) => passage.text.toLowerCase());
  const weights = termWeights(lowered, terms);
  const scored: ScoredPassage[] = [];
  for (const [order, passage] of passages.entries()) {
    const found = match(lowered[order], phrase, weights);
    if (found !== null) {
      scored.push({ passage, order, ...found });
    }
  }
  return scored
    .sort((a, b) => b.score - a.score || b.order - a.order)
    .slice(0, MAX_HITS)
    .map((hit) => ({
      turn: hit.passage.turn,
      at: hit.passage.start + hit.matchAt,
      excerpt: excerptAround(hit.passage.text, hit.matchAt),
    }));
}
