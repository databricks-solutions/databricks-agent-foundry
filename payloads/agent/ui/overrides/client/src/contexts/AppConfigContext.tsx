import { createContext, useContext, type ReactNode } from 'react';
import useSWR from 'swr';
import { chatWidthClass } from '@/lib/chat-width';
import { fetcher } from '@/lib/utils';

interface FeedbackCategory {
  id: string;
  label: string;
}

interface ConfigResponse {
  features: {
    chatHistory: boolean;
    feedback: boolean;
    fileUpload: boolean;
    imageInput: boolean;
    charts: boolean;
    toolOutputExpanded: boolean;
    chatWidth?: string;
    resourcesSidebar: boolean;
    compaction: boolean;
    compactionManual: boolean;
    plan: boolean;
    speechInput?: boolean;
    speechMaxSeconds?: number;
    speechLanguage?: string;
  };
  obo?: {
    missingScopes: string[];
  };
  feedbackCategories?: FeedbackCategory[];
}

interface AppConfigContextType {
  config: ConfigResponse | undefined;
  isLoading: boolean;
  error: Error | undefined;
  chatHistoryEnabled: boolean;
  feedbackEnabled: boolean;
  fileUploadEnabled: boolean;
  imageInputEnabled: boolean;
  chartsEnabled: boolean;
  toolOutputExpanded: boolean;
  /** Tailwind `max-w-*` class capping the chat column. */
  chatWidthClass: string;
  resourcesSidebarEnabled: boolean;
  compactionEnabled: boolean;
  compactionManualEnabled: boolean;
  planEnabled: boolean;
  speechInputEnabled: boolean;
  /** Longest recording, in seconds. */
  speechMaxSeconds: number;
  /** Instance language hint; empty means the browser language. */
  speechLanguage: string;
  oboMissingScopes: string[];
  feedbackCategories: FeedbackCategory[];
}

const AppConfigContext = createContext<AppConfigContextType | undefined>(
  undefined,
);

export function AppConfigProvider({ children }: { children: ReactNode }) {
  const { data, error, isLoading } = useSWR<ConfigResponse>(
    '/api/config',
    fetcher,
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      dedupingInterval: 60000,
    },
  );

  const value: AppConfigContextType = {
    config: data,
    isLoading,
    error,
    chatHistoryEnabled: data?.features.chatHistory ?? true,
    feedbackEnabled: data?.features.feedback ?? false,
    fileUploadEnabled: data?.features.fileUpload ?? false,
    imageInputEnabled: data?.features.imageInput ?? false,
    chartsEnabled: data?.features.charts ?? false,
    // Pre-fetch fallback matches the spec default (collapsed), so panels
    // rendered before /api/config resolves do not flash open.
    toolOutputExpanded: data?.features.toolOutputExpanded ?? false,
    chatWidthClass: chatWidthClass(data?.features.chatWidth),
    resourcesSidebarEnabled: data?.features.resourcesSidebar ?? false,
    compactionEnabled: data?.features.compaction ?? false,
    compactionManualEnabled: data?.features.compactionManual ?? false,
    planEnabled: data?.features.plan ?? false,
    speechInputEnabled: data?.features.speechInput ?? false,
    speechMaxSeconds: data?.features.speechMaxSeconds ?? 300,
    speechLanguage: data?.features.speechLanguage ?? '',
    oboMissingScopes: data?.obo?.missingScopes ?? [],
    feedbackCategories: data?.feedbackCategories ?? [],
  };

  return (
    <AppConfigContext.Provider value={value}>
      {children}
    </AppConfigContext.Provider>
  );
}

export function useAppConfig() {
  const context = useContext(AppConfigContext);
  if (context === undefined) {
    throw new Error('useAppConfig must be used within an AppConfigProvider');
  }
  return context;
}
