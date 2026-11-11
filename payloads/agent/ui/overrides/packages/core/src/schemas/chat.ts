import { z } from 'zod';

const textPartSchema = z.object({
  type: z.enum(['text']),
  text: z.string().min(1),
});

// Agent Bricks file-upload override (file-upload-feature.md D7/D8).
// Upstream only accepted image attachments (`mediaType` image/jpeg|png with a
// fetchable `url`). The file-upload capability also sends extracted-document
// parts, so this:
//   - relaxes `mediaType` to any non-empty string,
//   - relaxes `url` to any non-empty string (documents carry a synthetic
//     `upload://<id>` instead of a fetchable URL; `url` stays required because
//     the AI SDK `FileUIPart` type mandates a string url),
//   - adds `extractedText` / `truncated` / `format`, the synchronous
//     extraction result that chat-turn.ts injects into the model request and that
//     is persisted on the message for re-injection on later turns.
// Image parts continue to validate unchanged (they have a `url` and no
// `extractedText`).
const filePartSchema = z.object({
  type: z.enum(['file']),
  mediaType: z.string().min(1),
  name: z.string().min(1),
  // AI SDK FileUIPart standard label; mirrored from `name` so the rendered
  // attachment chip keeps the filename after server-side validation strips
  // unknown keys.
  filename: z.string().optional(),
  url: z.string().min(1),
  extractedText: z.string().optional(),
  truncated: z.boolean().optional(),
  format: z.string().optional(),
});

const partSchema = z.union([textPartSchema, filePartSchema]);

// Schema for previous messages in ephemeral mode
// More permissive to handle various message types (user, assistant, tool calls, etc.)
const previousMessageSchema = z.object({
  // Accept any string ID: user messages have UUIDs, but assistant messages
  // from the AI SDK use nanoid (16-char alphanumeric), not UUIDs.
  id: z.string(),
  role: z.enum(['user', 'assistant', 'system']),
  parts: z.array(z.any()), // Permissive to handle text, tool calls, tool results
});

export const postRequestBodySchema = z.object({
  id: z.string().uuid(),
  // Optional for continuation/regeneration scenarios (no new user message)
  message: z
    .object({
      id: z.string().uuid(),
      role: z.enum(['user']),
      parts: z.array(partSchema),
    })
    .optional(),
  selectedChatModel: z.enum(['chat-model', 'chat-model-reasoning']),
  selectedVisibilityType: z.enum(['public', 'private']),
  // Optional field for ephemeral mode: frontend sends previous conversation history
  previousMessages: z.array(previousMessageSchema).optional(),
});

export type PostRequestBody = z.infer<typeof postRequestBodySchema>;
