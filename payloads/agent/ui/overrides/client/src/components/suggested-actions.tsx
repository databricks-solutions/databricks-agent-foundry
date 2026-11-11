import { motion } from 'framer-motion';
import { memo } from 'react';
import { Suggestion } from './elements/suggestion';
import { LightbulbIcon } from '@/components/icons';
import { softNavigateToChatId } from '@/lib/navigation';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { chatUiExtensions } from '@/extensions/registry';
import type { SuggestedActionsProps } from '@/extensions/types';

function DefaultSuggestedActions({ chatId, sendMessage }: SuggestedActionsProps) {
  const { chatHistoryEnabled } = useAppConfig();
  const suggestedActions = [
    'How can you help me?',
    'Tell me something I might not know',
  ];

  return (
    <div
      data-testid="suggested-actions"
      className="flex w-full flex-col"
    >
      {suggestedActions.map((suggestedAction, index) => (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 20 }}
          transition={{ delay: 0.05 * index }}
          key={suggestedAction}
          className="border-b border-border"
        >
          <Suggestion
            suggestion={suggestedAction}
            variant="tertiary"
            onClick={(suggestion) => {
              softNavigateToChatId(chatId, chatHistoryEnabled);
              sendMessage({
                role: 'user',
                parts: [{ type: 'text', text: suggestion }],
              });
            }}
            className="h-auto w-full justify-start gap-2 rounded-none border-0 bg-transparent py-2 pl-0 text-left text-sm font-normal text-muted-foreground hover:bg-transparent hover:text-foreground"
          >
            <LightbulbIcon size={16} className="shrink-0 text-muted-foreground" aria-hidden />
            {suggestedAction}
          </Suggestion>
        </motion.div>
      ))}
    </div>
  );
}

function PureSuggestedActions(props: SuggestedActionsProps) {
  const SuggestedActionsSlot = chatUiExtensions.SuggestedActions;
  const fallback = <DefaultSuggestedActions {...props} />;
  return SuggestedActionsSlot ? <SuggestedActionsSlot {...props} fallback={fallback} /> : fallback;
}

export const SuggestedActions = memo(
  PureSuggestedActions,
  (prevProps, nextProps) => {
    if (prevProps.chatId !== nextProps.chatId) return false;
    if (prevProps.selectedVisibilityType !== nextProps.selectedVisibilityType)
      return false;

    return true;
  },
);
