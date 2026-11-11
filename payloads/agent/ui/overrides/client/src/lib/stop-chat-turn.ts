/**
 * Asks the server to stop the chat's running turn (#775): only `streamId`
 * when the transport reported it, else every running turn in the chat.
 * Fire-and-forget; the UI has already stopped reading.
 */
export function requestTurnStop(chatId: string, streamId?: string): void {
  const query = streamId ? `?streamId=${encodeURIComponent(streamId)}` : '';
  fetch(`/api/chat/${encodeURIComponent(chatId)}/stream${query}`, {
    method: 'DELETE',
    credentials: 'include',
  }).catch((error: unknown) => {
    console.warn('[Chat] Stop request failed:', error);
  });
}
