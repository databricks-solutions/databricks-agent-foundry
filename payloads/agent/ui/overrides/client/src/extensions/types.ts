import type { ComponentType, ReactElement, ReactNode } from 'react';
import type { UseChatHelpers } from '@ai-sdk/react';
import type { ChatMessage } from '@chat-template/core';
import type { VisibilityType } from '@/components/visibility-selector';

export interface ExtensionRoute {
  path: string;
  element: ReactElement;
}

export interface SidebarNavProps {
  open: boolean;
  closeMobile: () => void;
}

export interface SuggestedActionsProps {
  chatId: string;
  sendMessage: UseChatHelpers<ChatMessage>['sendMessage'];
  setMessages: UseChatHelpers<ChatMessage>['setMessages'];
  selectedVisibilityType: VisibilityType;
}

export interface SuggestedActionsSlotProps extends SuggestedActionsProps {
  /** The stock suggestions, for when the slot has nothing of its own to show. */
  fallback: ReactNode;
}

export type ChatEffect = (chatId: string, messages: ChatMessage[]) => void;

/**
 * Extension points of the agent chat UI. An agent extension's UI overlay
 * replaces `registry.tsx` (never this file) to fill them in.
 *
 * Core components read `chatUiExtensions` at render time, never at module
 * load: an overlay registry may import core components (e.g. `Chat`), and
 * that cycle leaves the registry uninitialized while those modules load.
 */
export interface ChatUiExtensions {
  /** Extra routes rendered inside the chat layout. */
  routes: ExtensionRoute[];
  /** Extra entries in the sidebar menu, below "New chat". */
  SidebarNav?: ComponentType<SidebarNavProps>;
  /** Extra sidebar content below the chat history. */
  SidebarSection?: ComponentType;
  /** Replaces the default new-chat suggestions. */
  SuggestedActions?: ComponentType<SuggestedActionsSlotProps>;
  /** Runs inside every chat; must be a stable hook. */
  useChatEffect?: ChatEffect;
}
