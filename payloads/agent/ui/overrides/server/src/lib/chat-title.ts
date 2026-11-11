import { generateText } from 'ai';
import { type ChatMessage, myProvider } from '@chat-template/core';

// ---------------------------------------------------------------------------
// Title generation — Agent Bricks override
// ---------------------------------------------------------------------------
// Short messages (≤60 chars) are used directly as the sidebar title.
// Longer messages are summarised by the configured title model with a
// hardened prompt that prevents reasoning / chain-of-thought output.

const TITLE_SHORT_THRESHOLD = 60;
const TITLE_MAX_LENGTH = 80;

const TITLE_SYSTEM_PROMPT = [
 'You are a title generator. Output ONLY a short title (max 80 characters) that summarises the user message.',
 'Rules:',
 '- Output the title text only — no quotes, colons, preamble, or explanation.',
 '- Do NOT reason, think, or reflect. Do NOT use <think> tags or chain-of-thought.',
 '- Do NOT start with "Sure", "Here", "I" or any filler.',
 '- If the message is already concise, paraphrase it minimally.',
].join('\n');

export async function generateTitleFromUserMessage({
 message,
 maxMessageLength = 256,
}: {
 message: ChatMessage;
 maxMessageLength?: number;
}) {
 const textPart = message.parts.find((part) => part.type === 'text');
 const userText = textPart && 'text' in textPart ? textPart.text.trim() : '';

 if (userText.length > 0 && userText.length <= TITLE_SHORT_THRESHOLD) {
 return truncatePreserveWords(userText, TITLE_MAX_LENGTH);
 }

 const model = await myProvider.languageModel('title-model');

 const truncatedMessage = {
 ...message,
 parts: message.parts.map((part) =>
 part.type === 'text'
 ? { ...part, text: part.text.slice(0, maxMessageLength) }
 : part,
 ),
 };

 const { text: title } = await generateText({
 model,
 system: TITLE_SYSTEM_PROMPT,
 prompt: JSON.stringify(truncatedMessage),
 // No `temperature`: Claude 5.x endpoints reject the parameter.
 });

 return truncatePreserveWords(title.replace(/^["']|["']$/g, ''), TITLE_MAX_LENGTH);
}

export function truncatePreserveWords(input: string, maxLength: number): string {
 if (maxLength <= 0) return '';
 if (input.length <= maxLength) return input;

 const slice = input.slice(0, maxLength);
 const lastSpaceIndex = slice.lastIndexOf(' ');

 if (lastSpaceIndex <= 0) {
 return slice;
 }

 return slice.slice(0, lastSpaceIndex);
}
