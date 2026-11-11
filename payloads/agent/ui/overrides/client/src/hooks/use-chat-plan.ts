import { useEffect, useRef } from 'react';
import useSWR from 'swr';
import type { ChatMessage } from '@chat-template/core';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { type ChatPlan, finishedPlanCalls, parsePlan } from '@/lib/chat-plan';

/** As `GET /api/chat/:id/plan` returns it. */
interface ChatPlanResponse {
  content: string | null;
}

function chatPlanKey(chatId: string): string {
  return `/api/chat/${encodeURIComponent(chatId)}/plan`;
}

async function getChatPlan(url: string): Promise<ChatPlanResponse> {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<ChatPlanResponse>;
}

/** The chat's plan, refetched each time the agent finishes a plan update; `null` without one. */
export function useChatPlan(chatId: string, messages: readonly ChatMessage[]): ChatPlan | null {
  const { planEnabled } = useAppConfig();
  const { data, mutate } = useSWR<ChatPlanResponse>(
    planEnabled ? chatPlanKey(chatId) : null,
    getChatPlan,
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const planCalls = finishedPlanCalls(messages);
  const seenPlanCalls = useRef(planCalls);
  useEffect(() => {
    if (planEnabled && planCalls !== seenPlanCalls.current) {
      seenPlanCalls.current = planCalls;
      void mutate();
    }
  }, [planEnabled, planCalls, mutate]);
  return data?.content ? parsePlan(data.content) : null;
}
