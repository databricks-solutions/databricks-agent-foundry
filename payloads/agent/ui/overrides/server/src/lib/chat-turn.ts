import type { IncomingHttpHeaders } from 'node:http';
import {
 convertToModelMessages,
 createUIMessageStream,
 streamText,
 type LanguageModelUsage,
 type UIMessageChunk,
} from 'ai';
import type { LanguageModelV3Usage } from '@ai-sdk/provider';

// Convert ai's LanguageModelUsage to @ai-sdk/provider's LanguageModelV3Usage
function toV3Usage(usage: LanguageModelUsage): LanguageModelV3Usage {
 return {
 inputTokens: {
 total: usage.inputTokens,
 noCache: undefined,
 cacheRead: undefined,
 cacheWrite: undefined,
 },
 outputTokens: {
 total: usage.outputTokens,
 text: undefined,
 reasoning: undefined,
 },
 };
}
import type { AuthSession } from '@chat-template/auth';
import {
 getMessagesByChatId,
 saveChat,
 saveMessages,
 updateChatLastContextById,
 isDatabaseAvailable,
 updateChatTitleById,
} from '@chat-template/db';
import {
 type ChatMessage,
 type ContextWindowReading,
 checkChatAccess,
 convertToUIMessages,
 generateUUID,
 myProvider,
 postRequestBodySchema,
 type PostRequestBody,
 StreamCache,
 type VisibilityType,
 CONTEXT_HEADER_CONVERSATION_ID,
 CONTEXT_HEADER_USER_ID,
} from '@chat-template/core';
import { ChatSDKError } from '@chat-template/core/errors';
import {
 CONTEXT_HEADER_REASONING_REPLAY,
 CONTEXT_HEADER_SELECTED_RESOURCES,
 CONTEXT_HEADER_USER_SKILLS,
 encodeHeaderJson,
} from '@chat-template/ai-sdk-providers';
import { storeMessageMeta } from './message-meta-store';
import { requestPicks } from './request-picks';
import { saveAgentSkills } from './agent-skill-saves';
import { drainStreamToWriter, fallbackToGenerateText } from './stream-fallback';
import { sanitizeToolPairs, toolCallIds } from './sanitize-tool-pairs';
import {
 REASONING_REPLAY_KEY,
 entriesForRequest,
 replayEntriesFrom,
 type ReplayEntry,
} from './reasoning-replay';
import { reasoningReplayStore, replayEntriesForChat } from './reasoning-replay-store';
import { inlineFileAttachments } from './inline-file-attachments';
import { omitChartData } from './chart-fence-placeholder';
import { withoutRefusedTurns } from './refused-turns';
import {
 applyCompaction,
 compactableSpan,
 contextReading,
 contextWindowFromRaw,
 customOutputFromRaw,
 type ContextWindowReport,
} from './compaction';
import { compactionSettings, compactionStore } from './compaction-store';
import { compactInBackground, compactionForRequest } from './compaction-runner';
import { generateTitleFromUserMessage, truncatePreserveWords } from './chat-title';
import { TurnAborts } from './turn-aborts';

const streamCache = new StreamCache<UIMessageChunk>();
const turnAborts = new TurnAborts();

/** Who is asking, as resolved from the request (HTTP) or the upgrade (WebSocket). */
export interface ChatTurnContext {
 session: AuthSession | undefined;
 /** Only the forwarded identity headers are read. */
 headers: IncomingHttpHeaders;
}

export interface ChatTurn {
 streamId: string;
 stream: ReadableStream<UIMessageChunk>;
}

/** Any error a turn raises, as the ChatSDKError the client understands. */
export function asChatSDKError(error: unknown): ChatSDKError {
 return error instanceof ChatSDKError ? error : new ChatSDKError('offline:chat');
}

function cachedStream(streamId: string): ReadableStream<UIMessageChunk> {
 const stream = streamCache.getStream(streamId);
 if (!stream) throw new ChatSDKError('empty:stream');
 return stream;
}

/**
 * Runs one chat turn: saves the user message, streams the agent's reply
 * and saves it when the stream ends.
 *
 * Works in ephemeral mode when database is disabled.
 * Streaming continues normally, but no chat/message persistence occurs.
 */
export async function startChatTurn(
 ctx: ChatTurnContext,
 rawBody: unknown,
): Promise<ChatTurn> {
 const dbAvailable = isDatabaseAvailable();
 if (!dbAvailable) {
 console.log('[Chat] Running in ephemeral mode - no persistence');
 }

 let requestBody: PostRequestBody;

 try {
 requestBody = postRequestBodySchema.parse(rawBody);
 } catch (_) {
 console.error('Error parsing request body:', _);
 throw new ChatSDKError('bad_request:api');
 }

 try {
 const {
 id,
 message,
 selectedChatModel,
 selectedVisibilityType,
 }: {
 id: string;
 message?: ChatMessage;
 selectedChatModel: string;
 selectedVisibilityType: VisibilityType;
 } = requestBody;

 const session = ctx.session;
 if (!session) {
 throw new ChatSDKError('unauthorized:chat');
 }

 // Agent Bricks: kicked off here, not awaited until requestHeaders is
 // built. requestPicks only needs the user id, so it overlaps with
 // the chat-access / history / save-message round trips below instead
 // of adding its own round trip after them. Never rejects -- see its
 // own try/catch.
 const picksPromise = requestPicks(session.user.id);

 const { chat, allowed, reason } = await checkChatAccess(
 id,
 session?.user.id,
 );

 if (reason !== 'not_found' && !allowed) {
 throw new ChatSDKError('forbidden:chat');
 }

 let titlePromise: Promise<string | null> | undefined;

 if (!chat) {
 // Only create new chat if we have a message (not a continuation)
 if (isDatabaseAvailable() && message) {
 await saveChat({
 id,
 userId: session.user.id,
 title: 'New chat',
 visibility: selectedVisibilityType,
 });

 titlePromise = generateTitleFromUserMessage({ message })
 .then(async (title) => {
 await updateChatTitleById({ chatId: id, title });
 return title;
 })
 .catch(async (error) => {
 console.error('Error generating title:', error);
 const textFromUserMessage = message?.parts.find(
 (part) => part.type === 'text',
 )?.text;
 if (textFromUserMessage) {
 const fallback = truncatePreserveWords(
 textFromUserMessage,
 128,
 );
 await updateChatTitleById({ chatId: id, title: fallback });
 return fallback;
 }
 return null;
 });
 }
 } else {
 if (chat.userId !== session.user.id) {
 throw new ChatSDKError('forbidden:chat');
 }
 }

 // Agent Bricks (#637): the chat's latest compaction summary, read while
 // the history loads below. Never rejects -- see its own try/catch.
 const compactionPromise = compactionForRequest(id);
 // Agent Bricks (#681): the chat's stored reasoning replay entries, read
 // alongside. Never rejects -- see replayEntriesForChat.
 const replayStore = reasoningReplayStore();
 const storedReplayPromise = replayStore ? replayEntriesForChat(replayStore, id) : null;

 // Use previousMessages from request body when:
 // 1. Ephemeral mode (DB not available) - always use client-side messages
 // 2. Continuation request (no message) - tool results only exist client-side
 // Agent Bricks: getMessagesByChatId only runs for the else branch now --
 // it used to run unconditionally and get thrown away on every ephemeral
 // or continuation request, paying a DB round trip for nothing.
 const useClientMessages =
 !dbAvailable || (!message && requestBody.previousMessages);
 const previousMessages = useClientMessages
 ? (requestBody.previousMessages ?? [])
 : convertToUIMessages(await getMessagesByChatId({ id }));

 // If message is provided, add it to the list and save it
 // If not (continuation/regeneration), just use previous messages
 let uiMessages: ChatMessage[];
 if (message) {
 uiMessages = [...previousMessages, message];
 await saveMessages({
 messages: [
 {
 chatId: id,
 id: message.id,
 role: 'user',
 parts: message.parts,
 attachments: [],
 createdAt: new Date(),
 traceId: null,
 },
 ],
 });
 } else {
 // Continuation: use existing messages without adding new user message
 uiMessages = previousMessages as ChatMessage[];

// For continuations with database enabled, save any updated assistant messages
// so tool-result parts are persisted across turns.
if (dbAvailable && requestBody.previousMessages) {
const assistantMessages = requestBody.previousMessages.filter(
(m: ChatMessage) => m.role === 'assistant',
);
if (assistantMessages.length > 0) {
await saveMessages({
messages: assistantMessages.map((m: ChatMessage) => ({
chatId: id,
id: m.id,
role: m.role,
parts: m.parts,
attachments: [],
createdAt: m.metadata?.createdAt
? new Date(m.metadata.createdAt)
: new Date(),
traceId: null,
})),
});
}
}
 }

 // Clear any previous active stream for this chat
 streamCache.clearActiveStream(id);

 let finalUsage: LanguageModelUsage | undefined;
 let traceId: string | null = null;
 let contextWindow: ContextWindowReport | null = null;
 let reading: ContextWindowReading | null = null;
 let producedReplay: ReplayEntry[] = [];
 const streamId = generateUUID();

 const model = await myProvider.languageModel(selectedChatModel);

 // Issue #297: a duplicate `toolCallId` across UI parts (e.g. a stale
 // input-available part followed by a fresh output-available one)
 // makes convertToModelMessages emit two `tool_use` blocks with the
 // same id; Anthropic / Bedrock then rejects the request with
 // "unexpected tool_use_id found in tool_result blocks".
 // sanitizeToolPairs de-duplicates by id, keeping the most-resolved
 // part. `ignoreIncompleteToolCalls: true` is the SDK's own guard
 // against the orthogonal "lone unresolved tool part" failure mode.
 const sanitized = sanitizeToolPairs(uiMessages);
 if (sanitized.droppedDuplicates.length > 0) {
 console.warn(
 `[sanitize] dropped ${sanitized.droppedDuplicates.length} duplicate ` +
 `tool-call part(s); sample call_ids=${JSON.stringify(
 sanitized.droppedDuplicates.slice(0, 5),
 )} chat_id=${id}`,
 );
 }
 uiMessages = sanitized.messages;
 // Agent Bricks (D7/D8): swap extracted-file parts for injected text for the
 // model request only. uiMessages keeps the original file part so the UI chip
 // persists and the text is re-injected on later turns.
 // Agent Bricks (#637): likewise, the compaction summary replaces the turns
 // before its boundary for the model request only, and refused turns are
 // left out so a refused prompt is not sent again.
 // Agent Bricks: chart fences in earlier assistant turns become a short
 // placeholder, so their inline data is not re-sent on every turn.
 const compaction = await compactionPromise;
 const modelUiMessages = omitChartData(
 inlineFileAttachments(
 applyCompaction(withoutRefusedTurns(uiMessages, compaction?.boundaryMessageId), compaction),
 ),
 );
 const modelMessages = await convertToModelMessages(modelUiMessages, {
 ignoreIncompleteToolCalls: true,
 });
 // Agent Bricks (#681): only entries whose tool calls are in this request,
 // so compacted turns drop out. An empty list still opts the agent in.
 const replay = storedReplayPromise
 ? entriesForRequest(await storedReplayPromise, toolCallIds(modelUiMessages))
 : null;
 const { resources: picks, userSkills } = await picksPromise;
 const requestHeaders = {
 [CONTEXT_HEADER_CONVERSATION_ID]: id,
 [CONTEXT_HEADER_USER_ID]: session.user.email ?? session.user.id,
 // Forward OBO user token to the backend/serving endpoint
 ...(ctx.headers['x-forwarded-access-token']
 ? { 'x-forwarded-access-token': ctx.headers['x-forwarded-access-token'] as string }
 : {}),
 // Forward the gateway identity so the agent and routes the browser calls
 // directly resolve the same caller.
 ...(ctx.headers['x-forwarded-email']
 ? { 'x-forwarded-email': ctx.headers['x-forwarded-email'] as string }
 : {}),
 // Agent Bricks: agent memory is keyed on this id, the chat's userId
 ...(ctx.headers['x-forwarded-user']
 ? { 'x-forwarded-user': ctx.headers['x-forwarded-user'] as string }
 : {}),
 // Agent Bricks: sidebar picks -> custom_inputs.selected_resources
 ...(picks ? { [CONTEXT_HEADER_SELECTED_RESOURCES]: encodeHeaderJson(picks) } : {}),
 // Agent Bricks: picked Lakebase skills -> custom_inputs.user_skills
 ...(userSkills ? { [CONTEXT_HEADER_USER_SKILLS]: encodeHeaderJson(userSkills) } : {}),
 // Agent Bricks (#681): stored replay entries -> custom_inputs.reasoning_replay
 ...(replay ? { [CONTEXT_HEADER_REASONING_REPLAY]: encodeHeaderJson(replay) } : {}),
 };

 const abortSignal = turnAborts.start(streamId, id, session.user.id);
 const result = streamText({
 model,
 messages: modelMessages,
 abortSignal,
 providerOptions: {
 databricks: { includeTrace: true },
 },
 includeRawChunks: true,
 headers: requestHeaders,
 onChunk: ({ chunk }) => {
 if (chunk.type === 'raw') {
 const raw = chunk.rawValue as any;
 // Extract trace in Databricks serving endpoint output format, if present
 if (raw?.type === 'response.output_item.done') {
 const traceIdFromChunk =
 raw?.databricks_output?.trace?.info?.trace_id;
 if (typeof traceIdFromChunk === 'string') {
 traceId = traceIdFromChunk;
 }
 }
 // Extract trace from MLflow AgentServer output format, if present
 if (!traceId && typeof raw?.trace_id === 'string') {
 traceId = raw.trace_id;
 }
 // Agent Bricks (#637): the agent's prompt-size estimate for this turn
 contextWindow = contextWindowFromRaw(raw) ?? contextWindow;
 // Agent Bricks (#681): this run's replay entries, stored in onFinish
 producedReplay = replayEntriesFrom(customOutputFromRaw(raw, REASONING_REPLAY_KEY)) ?? producedReplay;
 }
 },
 onFinish: ({ usage }) => {
 finalUsage = usage;
 },
 });

 /**
 * We manually read from toUIMessageStream instead of using writer.merge
 * so the execute promise (and thus the outer stream) stays alive if we
 * need to fall back to generateText after a streaming error.
 */
 const stream = createUIMessageStream({
 // Pass originalMessages so that continuation responses reuse the existing
 // assistant message ID. Without this, handleUIMessageStreamFinish generates
 // a fresh ID, causing the client to push a second assistant message instead
 // of replacing the existing one.
 originalMessages: uiMessages,
 // The DB Message.id column is typed as uuid, so we must generate UUIDs
 // rather than the AI SDK's default short-id format (e.g. "Xt8nZiQRj1fS4yiU").
 generateId: generateUUID,
 execute: async ({ writer }) => {
 // Manually drain the AI stream so we can append the traceId data part
 // after all model chunks are processed (traceId is captured via onChunk).
 // result.toUIMessageStream() converts TextStreamPart → UIMessageChunk:
 // - text-delta: maps TextStreamPart.text → UIMessageChunk.delta
 // - start-step/finish-step: strips extra fields
 // - finish: strips rawFinishReason/totalUsage
 // - raw: dropped (trace_id captured via onChunk above)
 const aiStream = result.toUIMessageStream ({
 sendReasoning: true,
 sendSources: true,
 sendFinish: false,
 onError: (error) => {
 const msg =
 error instanceof Error ? error.message : String(error);
 writer.onError?.(error);
 return msg;
 },
 });

 const { failed } = await drainStreamToWriter(aiStream, writer);

 // A stopped turn gets no retry, which would run the agent again,
 // and saves none of the agent's skill writes.
 const stopped = abortSignal.aborted;
 if (failed && !stopped) {
 console.log('Streaming failed, falling back to generateText...');
 const fallbackResult = await fallbackToGenerateText(
 { model, messages: modelMessages, headers: requestHeaders },
 writer,
 );

 finalUsage = fallbackResult?.usage;
 traceId = fallbackResult?.traceId ?? null;
 } else if (!stopped) {
 // Agent Bricks: the agent's _save_skill calls, saved as the user (#642)
 for (const saved of await saveAgentSkills(session.user.id, result.steps)) {
 writer.write({ type: 'data-userSkillSaved', data: saved });
 }
 }
 if (titlePromise) {
 const generatedTitle = await titlePromise;
 if (generatedTitle) {
 writer.write({ type: 'data-title', data: generatedTitle });
 }
 }

 // Agent Bricks (#637): the context meter's reading for this turn
 if (compactionStore()) {
 const settings = compactionSettings();
 reading = contextReading(
 contextWindow,
 settings.triggerTokens,
 compactableSpan(uiMessages, compaction, settings.keepRecentTurns) !== null,
 );
 if (reading) {
 writer.write({ type: 'data-contextWindow', data: reading });
 }
 }

 // Write traceId so the client knows whether feedback is supported.
 writer.write({ type: 'data-traceId', data: traceId });
 // The client resumes any turn whose last part is not `finish`.
 writer.write({ type: 'finish' });
 },
 onFinish: async ({ responseMessage }) => {
 // Store in-memory for ephemeral mode (also useful when DB is available)
 storeMessageMeta(responseMessage.id, id, traceId);

 let messageSaved = true;
 try {
 await saveMessages({
 messages: [
 {
 id: responseMessage.id,
 role: responseMessage.role,
 parts: responseMessage.parts,
 createdAt: new Date(),
 attachments: [],
 chatId: id,
 traceId, // Store trace ID for feedback
 },
 ],
 });
 } catch (err) {
 console.error('[onFinish] Failed to save assistant message:', err);
 messageSaved = false;
 }

 // Agent Bricks (#681): the entries reference the saved message row
 if (replayStore && messageSaved) {
 try {
 await replayStore.append(id, responseMessage.id, producedReplay);
 } catch (err) {
 console.warn('[onFinish] Unable to store reasoning replay for chat', id, err);
 }
 }

 if (finalUsage) {
 try {
 await updateChatLastContextById({
 chatId: id,
 context: toV3Usage(finalUsage),
 });
 } catch (err) {
 console.warn('Unable to persist last usage for chat', id, err);
 }
 }

 // Agent Bricks (#637): compact after the turn that crossed the trigger,
 // once its assistant message is saved, so the next turn is smaller.
 const store = compactionStore();
 if (store && reading?.compacting) {
 compactInBackground(store, id, reading.estimatedInputTokens);
 }

 streamCache.clearActiveStream(id, streamId);
 turnAborts.finish(streamId);
 },
 });

 // The cache drains the turn on its own, so it runs to the end and stays
 // resumable when the client disconnects.
 streamCache.storeStream({ streamId, chatId: id, stream });
 return { streamId, stream: cachedStream(streamId) };
 } catch (error) {
 console.error('[Chat] Caught error in chat API:', {
 errorType: error?.constructor?.name,
 message: error instanceof Error ? error.message : String(error),
 stack: error instanceof Error ? error.stack : undefined,
 error,
 });

 throw asChatSDKError(error);
 }
}

/**
 * Stops the user's running turns in the chat, or only `streamId`. Turns of
 * other users and unknown ids are ignored, so callers learn nothing from it.
 */
export function stopChatTurn(
 ctx: ChatTurnContext,
 chatId: string,
 streamId?: string,
): void {
 const userId = ctx.session?.user.id;
 if (!userId) throw new ChatSDKError('unauthorized:chat');
 const stopped = turnAborts.stop({ chatId, userId, streamId });
 if (stopped > 0) console.log(`[Chat] Stopped ${stopped} turn(s) for chat ${chatId}`);
}

/** Replays the chat's active stream from `cursor` (a chunk index). */
export async function resumeChatTurn(
 ctx: ChatTurnContext,
 chatId: string,
 cursor?: number,
): Promise<ChatTurn> {
 // Check if there's an active stream for this chat first
 const streamId = streamCache.getActiveStreamId(chatId);

 if (!streamId) {
 console.log(`[Stream Resume] No active stream for chat ${chatId}`);
 throw new ChatSDKError('empty:stream');
 }

 const { allowed, reason } = await checkChatAccess(
 chatId,
 ctx.session?.user.id,
 );

 // If chat doesn't exist in DB, it's a temporary chat from the homepage - allow it
 if (reason === 'not_found') {
 console.log(
 `[Stream Resume] Resuming stream for temporary chat ${chatId} (not yet in DB)`,
 );
 } else if (!allowed) {
 console.log(
 `[Stream Resume] User ${ctx.session?.user.id} does not have access to chat ${chatId} (reason: ${reason})`,
 );
 throw new ChatSDKError('forbidden:chat', reason);
 }

 // Get all cached chunks for this stream
 const stream = streamCache.getStream(streamId, { cursor });

 if (!stream) {
 console.log(`[Stream Resume] No stream found for ${streamId}`);
 throw new ChatSDKError('empty:stream');
 }

 console.log(`[Stream Resume] Resuming stream ${streamId}`);
 return { streamId, stream };
}
