import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { isDatabaseAvailable } from '@chat-template/db';
import { getEndpointOboInfo } from '@chat-template/ai-sdk-providers';
import { envFlag, positiveIntFromEnv } from '../lib/env-flag';
import { chatPlanEnabled } from '../lib/chat-plan-store';

export const configRouter: RouterType = Router();

function getScopesFromToken(token: string): string[] {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return [];
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf-8'));
    if (typeof payload.scope === 'string') return payload.scope.split(' ');
    if (Array.isArray(payload.scp)) return payload.scp as string[];
    return [];
  } catch {
    return [];
  }
}

const DEFAULT_FEEDBACK_CATEGORIES = [
  { id: 'wrong_answer', label: 'Wrong answer' },
  { id: 'missing_context', label: 'Missing context' },
  { id: 'hallucination', label: 'Hallucination' },
  { id: 'too_verbose', label: 'Too verbose' },
  { id: 'not_helpful', label: 'Not helpful' },
  { id: 'other', label: 'Other' },
];

function loadFeedbackCategories(): Array<{ id: string; label: string }> {
  const raw = process.env.FEEDBACK_CATEGORIES;
  if (!raw) return DEFAULT_FEEDBACK_CATEGORIES;
  try {
    return JSON.parse(raw);
  } catch {
    console.warn('[Config] Invalid FEEDBACK_CATEGORIES JSON, using defaults');
    return DEFAULT_FEEDBACK_CATEGORIES;
  }
}

// The agent payload's spec.ui.* toggles are rendered to these ENABLE_* env
// vars at deploy time (see payloads/agent/deploy/config.py:env_overrides_from_spec).

// Mirrors the spec.ui.file_upload toggle. The client uses this to show/hide
// the attach affordance so it never offers an upload the backend would reject
// with 501.
function isFileUploadEnabled(): boolean {
  return envFlag('ENABLE_FILE_UPLOAD');
}

// Mirrors spec.ui.file_upload.images (only true with file_upload on). Lets the
// composer accept images, which go to the model instead of the extractor.
function isImageInputEnabled(): boolean {
  return envFlag('ENABLE_IMAGE_INPUT');
}

// Mirrors spec.ui.charts. Gates the client chart-rendering fence so a
// chart-disabled deploy renders the spec as plain text instead.
function isChartsEnabled(): boolean {
  return envFlag('ENABLE_CHARTS');
}

// Mirrors spec.ui.feedback. Feedback additionally requires an MLflow
// experiment to log to, so both must be present for the affordance to show.
function isFeedbackEnabled(): boolean {
  return envFlag('ENABLE_FEEDBACK');
}

// Mirrors spec.ui.tool_output.expanded. Decides whether tool-call panels
// start expanded; unset reads as collapsed, which is the intended default.
function isToolOutputExpanded(): boolean {
  return envFlag('TOOL_OUTPUT_EXPANDED');
}

// Mirrors spec.ui.chat_layout.width. Passed through as-is: the client owns
// the allowed values and falls back to the default for anything unknown.
function chatMaxWidth(): string {
  return (process.env.CHAT_MAX_WIDTH ?? '').trim();
}

// Mirrors spec.ui.resources_sidebar. Shows the Unity Gateway resources panel.
function isResourcesSidebarEnabled(): boolean {
  return envFlag('ENABLE_RESOURCES_SIDEBAR');
}

// Mirrors spec.ui.speech_input. Without an endpoint the transcribe route
// answers 501, so the mic button stays hidden.
function isSpeechInputEnabled(): boolean {
  return envFlag('ENABLE_SPEECH_INPUT') && !!process.env.SPEECH_ENDPOINT?.trim();
}

// Mirrors spec.runtime.context.compaction; summaries need Lakebase.
function isCompactionEnabled(): boolean {
  return envFlag('ENABLE_COMPACTION') && isDatabaseAvailable();
}

// Mirrors spec.runtime.context.compaction.manual_action.
function isCompactionManualEnabled(): boolean {
  return isCompactionEnabled() && envFlag('COMPACTION_MANUAL_ACTION');
}
configRouter.get('/', async (req: Request, res: Response) => {
  const oboInfo = await getEndpointOboInfo();

  let missingScopes = oboInfo.endpointRequiredScopes;

  const userToken = req.headers['x-forwarded-access-token'] as string | undefined;
  if (userToken && oboInfo.isEndpointOboEnabled) {
    const tokenScopes = getScopesFromToken(userToken);
    missingScopes = oboInfo.endpointRequiredScopes.filter(required => {
      const parent = required.split('.')[0];
      return !tokenScopes.some(ts => ts === required || ts === parent);
    });
  }

  res.json({
    features: {
      chatHistory: isDatabaseAvailable(),
      feedback: isFeedbackEnabled() && !!process.env.MLFLOW_EXPERIMENT_ID,
      fileUpload: isFileUploadEnabled(),
      imageInput: isImageInputEnabled(),
      charts: isChartsEnabled(),
      toolOutputExpanded: isToolOutputExpanded(),
      chatWidth: chatMaxWidth(),
      resourcesSidebar: isResourcesSidebarEnabled(),
      compaction: isCompactionEnabled(),
      compactionManual: isCompactionManualEnabled(),
      // Mirrors spec.runtime.context.plan; shows the plan panel above the chat input.
      plan: chatPlanEnabled(),
      speechInput: isSpeechInputEnabled(),
      speechMaxSeconds: positiveIntFromEnv('SPEECH_MAX_SECONDS', 300),
      speechLanguage: (process.env.SPEECH_LANGUAGE ?? '').trim(),
    },
    obo: {
      missingScopes,
    },
    feedbackCategories: loadFeedbackCategories(),
  });
});
