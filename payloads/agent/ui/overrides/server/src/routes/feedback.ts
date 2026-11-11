import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import {
  getMessageById,
  voteMessage,
  getVotesByChatId,
} from '@chat-template/db';
import { checkChatAccess } from '@chat-template/core';
import { ChatSDKError } from '@chat-template/core/errors';
import { getDatabricksToken } from '@chat-template/auth';
import { getWorkspaceHostname } from '@chat-template/ai-sdk-providers';
import {
  getMessageMetadata,
  getAssessmentId,
  storeAssessmentId,
} from '../lib/message-meta-store';

// ---------------------------------------------------------------------------
// V4 trace ID helpers — mirrors mlflow.tracing.utils.parse_trace_id_v4
// ---------------------------------------------------------------------------

const TRACE_ID_V4_PREFIX = 'trace:/';

function parseTraceIdV4(
  traceId: string,
): { location: string; otelTraceId: string } | null {
  if (!traceId.startsWith(TRACE_ID_V4_PREFIX)) return null;
  const rest = traceId.slice(TRACE_ID_V4_PREFIX.length);
  const parts = rest.split('/');
  if (parts.length === 2 && parts[0] && parts[1]) {
    return { location: parts[0], otelTraceId: parts[1] };
  }
  return null;
}

function buildAssessmentUrl(
  hostUrl: string,
  traceId: string,
  assessmentId?: string,
): string {
  const v4 = parseTraceIdV4(traceId);
  const warehouseId = process.env.MLFLOW_TRACING_SQL_WAREHOUSE_ID;

  if (v4) {
    const base = `${hostUrl}/api/4.0/mlflow/traces/${v4.location}/${v4.otelTraceId}`;
    let url = assessmentId
      ? `${base}/assessments/${assessmentId}`
      : `${base}/assessments`;
    if (warehouseId) {
      url += `?sql_warehouse_id=${encodeURIComponent(warehouseId)}`;
    }
    return url;
  }

  const base = `${hostUrl}/api/3.0/mlflow/traces/${traceId}`;
  return assessmentId
    ? `${base}/assessments/${assessmentId}`
    : `${base}/assessments`;
}

// ---------------------------------------------------------------------------
// Reusable MLflow assessment helper
// ---------------------------------------------------------------------------

const VALID_FEEDBACK_TYPES = new Set([
  'thumbs_up',
  'thumbs_down',
  'comment',
  'categories',
]);

const COMMENT_MAX_LENGTH = 1000;

interface SubmitAssessmentParams {
  token: string;
  hostUrl: string;
  traceId: string;
  userId: string;
  assessmentName: string;
  feedbackValue: string | boolean;
  dedupKey: string;
  sessionUserId: string;
}

async function submitAssessment({
  token,
  hostUrl,
  traceId,
  userId,
  assessmentName,
  feedbackValue,
  dedupKey,
  sessionUserId,
}: SubmitAssessmentParams): Promise<string | undefined> {
  const existingAssessmentId = getAssessmentId(dedupKey, sessionUserId);
  const v4 = parseTraceIdV4(traceId);

  let mlflowResponse: globalThis.Response;
  if (existingAssessmentId) {
    const patchUrl = buildAssessmentUrl(hostUrl, traceId, existingAssessmentId);

    if (v4) {
      const finalUrl = patchUrl.includes('?')
        ? `${patchUrl}&update_mask=feedback`
        : `${patchUrl}?update_mask=feedback`;
      mlflowResponse = await fetch(finalUrl, {
        method: 'PATCH',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          assessment_name: assessmentName,
          trace_id: v4.otelTraceId,
          feedback: { value: feedbackValue },
        }),
      });
    } else {
      mlflowResponse = await fetch(patchUrl, {
        method: 'PATCH',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          assessment: {
            trace_id: traceId,
            assessment_name: assessmentName,
            feedback: { value: feedbackValue },
          },
          update_mask: 'feedback',
        }),
      });
    }
  } else if (v4) {
    mlflowResponse = await fetch(buildAssessmentUrl(hostUrl, traceId), {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        assessment_name: assessmentName,
        trace_id: v4.otelTraceId,
        source: { source_type: 'HUMAN', source_id: userId },
        feedback: { value: feedbackValue },
      }),
    });
  } else {
    mlflowResponse = await fetch(buildAssessmentUrl(hostUrl, traceId), {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        assessment: {
          trace_id: traceId,
          assessment_name: assessmentName,
          source: { source_type: 'HUMAN', source_id: userId },
          feedback: { value: feedbackValue },
        },
      }),
    });
  }

  if (!mlflowResponse.ok) {
    const errorText = await mlflowResponse.text();
    console.error(
      `Failed to submit ${assessmentName} to MLflow:`,
      errorText,
    );
    throw new Error(`MLflow ${mlflowResponse.status}: ${errorText}`);
  }

  const mlflowResult = await mlflowResponse.json();
  const assessmentId = mlflowResult.assessment?.assessment_id as
    | string
    | undefined;
  if (assessmentId) {
    storeAssessmentId(dedupKey, sessionUserId, assessmentId);
  }
  return assessmentId;
}

// ---------------------------------------------------------------------------

export const feedbackRouter: RouterType = Router();

feedbackRouter.use(authMiddleware);

/**
 * POST /api/feedback - Submit feedback for a message
 *
 * Body:
 * - messageId: string
 * - feedbackType: 'thumbs_up' | 'thumbs_down' | 'comment' | 'categories'
 * - comment?: string (required when feedbackType is 'comment', max 1000 chars)
 * - categories?: string[] (required when feedbackType is 'categories')
 */
feedbackRouter.post('/', requireAuth, async (req: Request, res: Response) => {
  try {
    const { messageId, feedbackType, comment, categories } = req.body;

    if (!messageId || !feedbackType || !VALID_FEEDBACK_TYPES.has(feedbackType)) {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    if (feedbackType === 'comment' && (typeof comment !== 'string' || !comment.trim())) {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    if (feedbackType === 'categories' && (!Array.isArray(categories) || categories.length === 0)) {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    const session = req.session;
    if (!session) {
      const error = new ChatSDKError('unauthorized:chat');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    const messages = await getMessageById({ id: messageId });
    let traceId: string | null;
    let chatId: string | undefined;

    if (!messages || messages.length === 0) {
      const metadata = getMessageMetadata(messageId);
      if (!metadata) {
        const error = new ChatSDKError('not_found:database');
        const response = error.toResponse();
        return res.status(response.status).json(response.json);
      }
      traceId = metadata.traceId;
      chatId = metadata.chatId;
    } else {
      const dbMessage = messages[0];
      traceId = dbMessage.traceId;
      chatId = dbMessage.chatId;
    }

    let mlflowAssessmentId: string | undefined;

    if (traceId) {
      try {
        const token = await getDatabricksToken();
        const hostUrl = await getWorkspaceHostname();
        const userId = session.user.email ?? session.user.id;

        if (feedbackType === 'thumbs_up' || feedbackType === 'thumbs_down') {
          mlflowAssessmentId = await submitAssessment({
            token,
            hostUrl,
            traceId,
            userId,
            assessmentName: 'user_feedback',
            feedbackValue: feedbackType === 'thumbs_up',
            dedupKey: messageId,
            sessionUserId: session.user.id,
          });
        } else if (feedbackType === 'comment') {
          const trimmed = comment.trim().slice(0, COMMENT_MAX_LENGTH);
          mlflowAssessmentId = await submitAssessment({
            token,
            hostUrl,
            traceId,
            userId,
            assessmentName: 'user_feedback_comment',
            feedbackValue: trimmed,
            dedupKey: `${messageId}:comment`,
            sessionUserId: session.user.id,
          });
        } else if (feedbackType === 'categories') {
          mlflowAssessmentId = await submitAssessment({
            token,
            hostUrl,
            traceId,
            userId,
            assessmentName: 'user_feedback_categories',
            feedbackValue: categories.join(','),
            dedupKey: `${messageId}:categories`,
            sessionUserId: session.user.id,
          });
        }
      } catch (error) {
        console.error('Error submitting feedback to MLflow:', error);
        const chatError = new ChatSDKError('offline:chat');
        const chatResponse = chatError.toResponse();
        return res.status(chatResponse.status).json(chatResponse.json);
      }
    } else {
      console.warn(
        'Message does not have a trace ID, skipping MLflow submission',
      );
    }

    // Persist thumbs to DB for fast bulk reads on page load
    if (chatId && (feedbackType === 'thumbs_up' || feedbackType === 'thumbs_down')) {
      try {
        await voteMessage({
          chatId,
          messageId,
          type: feedbackType === 'thumbs_up' ? 'up' : 'down',
        });
      } catch (err) {
        console.warn('[Feedback] DB vote save failed:', err);
      }
    }

    return res.status(200).json({
      success: true,
      mlflowAssessmentId,
    });
  } catch (error) {
    console.error('[Feedback] Error submitting feedback:', error);

    if (error instanceof ChatSDKError) {
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    const chatError = new ChatSDKError('offline:chat');
    const response = chatError.toResponse();
    return res.status(response.status).json(response.json);
  }
});

/**
 * GET /api/feedback/chat/:chatId - Get all feedback for a chat
 *
 * Reads votes from the DB for fast single-query page load.
 * Returns a map of messageId -> feedback.
 * Returns empty map if DB is unavailable (graceful degradation).
 */
feedbackRouter.get(
  '/chat/:chatId',
  requireAuth,
  async (req: Request, res: Response) => {
    try {
      const { chatId } = req.params;
      const session = req.session;
      if (!session) {
        return res.status(200).json({});
      }

      // Ownership check: verify the chat belongs to the requesting user
      const { allowed, reason } = await checkChatAccess(
        chatId as string,
        session.user.id,
      );
      if (reason !== 'not_found' && !allowed) {
        return res.status(200).json({});
      }

      const dbVotes = await getVotesByChatId({ id: chatId as string });
      type Feedback = {
        messageId: string;
        feedbackType: 'thumbs_up' | 'thumbs_down';
        assessmentId: null;
      };
      const feedbackMap: Record<string, Feedback> = {};
      for (const v of dbVotes) {
        feedbackMap[v.messageId] = {
          messageId: v.messageId,
          feedbackType: v.isUpvoted ? 'thumbs_up' : 'thumbs_down',
          assessmentId: null,
        };
      }

      return res.status(200).json(feedbackMap);
    } catch (error) {
      console.error('[Feedback] Error getting feedback by chat:', error);
      if (error instanceof ChatSDKError) {
        const response = error.toResponse();
        return res.status(response.status).json(response.json);
      }
      const chatError = new ChatSDKError('offline:chat');
      const response = chatError.toResponse();
      return res.status(response.status).json(response.json);
    }
  },
);
