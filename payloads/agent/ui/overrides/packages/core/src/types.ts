import { z } from 'zod';
import type { LanguageModelUsage, UIMessage } from 'ai';

const messageMetadataSchema = z.object({
  createdAt: z.string(),
});

type MessageMetadata = z.infer<typeof messageMetadataSchema>;

/**
 * Agent Bricks: the outcome of one `_save_skill` call by the agent, saved
 * as the signed-in user by the chat route (Lakebase user skills, #642).
 */
export type UserSkillSaved =
  | { ok: true; fqn: string; display_name: string; created: boolean }
  | { ok: false; slug: string | null; error: string };

/**
 * Agent Bricks: one turn's context-meter reading (#637), measured against
 * the compaction trigger rather than the model's window.  Persisted with
 * the assistant message, so the meter survives a reload.
 */
export interface ContextWindowReading {
  estimatedInputTokens: number | null;
  triggerTokens: number;
  /** A compaction was started after this turn. */
  compacting: boolean;
  /** ISO time of the reading; a newer compaction supersedes it. */
  measuredAt: string;
}

export type CustomUIDataTypes = {
  error: string;
  usage: LanguageModelUsage;
  traceId: string | null;
  title: string;
  userSkillSaved: UserSkillSaved;
  contextWindow: ContextWindowReading;
};

export type ChatMessage = UIMessage<MessageMetadata, CustomUIDataTypes>;

export interface Attachment {
  name: string;
  url: string;
  contentType: string;
  // Agent Bricks file upload (file-upload-feature.md D7/D8): the synchronous
  // extraction result carried on the attachment so submitForm can persist it
  // on the sent `file` part (and chat-turn.ts can re-inject it for the model).
  extractedText?: string;
  truncated?: boolean;
  format?: string;
}

export type { VisibilityType } from '@chat-template/utils';

export interface Feedback {
  messageId: string;
  feedbackType: 'thumbs_up' | 'thumbs_down';
  assessmentId: string | null;
}

export type FeedbackMap = Record<string, Feedback>;
