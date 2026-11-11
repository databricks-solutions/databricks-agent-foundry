import {
 Router,
 type Request,
 type Response,
 type Router as RouterType,
} from 'express';
import { pipeUIMessageStreamToResponse } from 'ai';
import {
 authMiddleware,
 requireAuth,
 requireChatAccess,
 getIdFromRequest,
} from '../middleware/auth';
import { deleteChatById, updateChatVisiblityById } from '@chat-template/db';
import { checkChatAccess } from '@chat-template/core';
import {
 asChatSDKError,
 resumeChatTurn,
 startChatTurn,
 stopChatTurn,
 type ChatTurnContext,
} from '../lib/chat-turn';
import { generateTitleFromUserMessage } from '../lib/chat-title';
import { withSsePadding } from '../lib/sse-padding';

export const chatRouter: RouterType = Router();

// Apply auth middleware to all chat routes
chatRouter.use(authMiddleware);

/**
 * POST /api/chat - Send a message and get streaming response
 *
 * Note: Works in ephemeral mode when database is disabled.
 * Streaming continues normally, but no chat/message persistence occurs.
 */
chatRouter.post('/', requireAuth, async (req: Request, res: Response) => {
 try {
 const { stream } = await startChatTurn(turnContext(req), req.body);
 pipeUIMessageStreamToResponse({ stream, response: withSsePadding(res) });
 } catch (error) {
 sendChatError(res, error);
 }
});

/**
 * DELETE /api/chat?id=:id - Delete a chat
 */
chatRouter.delete(
 '/:id',
 [requireAuth, requireChatAccess],
 async (req: Request, res: Response) => {
 const id = getIdFromRequest(req);
 if (!id) return;

 const deletedChat = await deleteChatById({ id });
 return res.status(200).json(deletedChat);
 },
);

/**
 * GET /api/chat/:id
 */

chatRouter.get(
 '/:id',
 [requireAuth, requireChatAccess],
 async (req: Request, res: Response) => {
 const id = getIdFromRequest(req);
 if (!id) return;

 const { chat } = await checkChatAccess(id, req.session?.user.id);

 return res.status(200).json(chat);
 },
);

/**
 * GET /api/chat/:id/stream - Resume a stream
 */
chatRouter.get(
 '/:id/stream',
 [requireAuth],
 async (req: Request, res: Response) => {
 const chatId = getIdFromRequest(req);
 if (!chatId) return;
 const cursor = req.headers['x-resume-stream-cursor'] as string;

 console.log(`[Stream Resume] Cursor: ${cursor}`);

 console.log(`[Stream Resume] GET request for chat ${chatId}`);

 try {
 const { stream } = await resumeChatTurn(
 turnContext(req),
 chatId,
 cursor ? Number.parseInt(cursor) : undefined,
 );
 pipeUIMessageStreamToResponse({ stream, response: res });
 } catch (error) {
 sendChatError(res, error);
 }
 },
);

/**
 * DELETE /api/chat/:id/stream[?streamId=] - Stop the user's running turn
 */
chatRouter.delete(
 '/:id/stream',
 [requireAuth],
 (req: Request, res: Response) => {
 const chatId = getIdFromRequest(req);
 if (!chatId) return;
 const { streamId } = req.query;
 stopChatTurn(
 turnContext(req),
 chatId,
 typeof streamId === 'string' ? streamId : undefined,
 );
 res.status(204).end();
 },
);

/**
 * POST /api/chat/title - Generate title from message
 */
chatRouter.post('/title', requireAuth, async (req: Request, res: Response) => {
 try {
 const { message } = req.body;
 const title = await generateTitleFromUserMessage({ message });
 res.json({ title });
 } catch (error) {
 console.error('Error generating title:', error);
 res.status(500).json({ error: 'Failed to generate title' });
 }
});

/**
 * PATCH /api/chat/:id/visibility - Update chat visibility
 */
chatRouter.patch(
 '/:id/visibility',
 [requireAuth, requireChatAccess],
 async (req: Request, res: Response) => {
 try {
 const id = getIdFromRequest(req);
 if (!id) return;
 const { visibility } = req.body;

 if (!visibility || !['public', 'private'].includes(visibility)) {
 return res.status(400).json({ error: 'Invalid visibility type' });
 }

 await updateChatVisiblityById({ chatId: id, visibility });
 res.json({ success: true });
 } catch (error) {
 console.error('Error updating visibility:', error);
 res.status(500).json({ error: 'Failed to update visibility' });
 }
 },
);

function turnContext(req: Request): ChatTurnContext {
 return { session: req.session, headers: req.headers };
}

function sendChatError(res: Response, error: unknown): void {
 const response = asChatSDKError(error).toResponse();
 res.status(response.status).json(response.json);
}
