import {
  useRef,
  useEffect,
  useState,
  useCallback,
  type Dispatch,
  type SetStateAction,
  type ChangeEvent,
  memo,
} from 'react';
import { toast } from 'sonner';
import { useLocalStorage, useWindowSize } from 'usehooks-ts';

import { PreviewAttachment } from './preview-attachment';
import { Button } from './ui/button';
import { SuggestedActions } from './suggested-actions';
import {
  PromptInput,
  PromptInputTextarea,
  PromptInputToolbar,
  PromptInputTools,
  PromptInputSubmit,
} from './elements/prompt-input';
import equal from 'fast-deep-equal';
import type { UseChatHelpers } from '@ai-sdk/react';
import { Paperclip } from 'lucide-react';
import type { VisibilityType } from './visibility-selector';
import type { Attachment, ChatMessage } from '@chat-template/core';
import { softNavigateToChatId } from '@/lib/navigation';
import { downscaleImage } from '@/lib/image-downscale';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { DbIcon } from './ui/db-icon';
import { ArrowUpIcon, StopIcon } from './icons';
import { ContextMeter } from './context-meter';
import { MicButton } from './mic-button';
import { insertTranscript } from '@/lib/speech-recorder';

// Agent Bricks file-upload override (file-upload-feature.md).
// Adds an attach button + drag/drop/paste that POST to the `/api/files/upload`
// proxy (server/src/routes/files.ts -> Python extractor). The extractor returns
// `{ name, format, extractedText, truncated }`; that result is carried on the
// attachment and persisted on the sent `file` part. The whole affordance is
// gated on `fileUploadEnabled` so it never appears when ENABLE_FILE_UPLOAD is
// off (the backend would otherwise answer 501).
const SUPPORTED_FILE_ACCEPT = '.xlsx,.csv,.docx,.pdf,.txt,.md,.json';

// Image input (`imageInputEnabled`, spec.ui.file_upload.images): images skip
// the extractor and travel inline as a data URL on the `file` part. They are
// downscaled first; the cap applies to the result and keeps the base64
// payload under the chat route's 10 MB JSON body limit.
const IMAGE_FILE_ACCEPT = '.png,.jpg,.jpeg,.webp';
const IMAGE_MEDIA_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

const NAVIGATING_DRAG_TYPES = ['Files', 'text/uri-list'];

// Outside the composer the browser would open a dropped file or link in place
// of the app (blocked by the app's CSP as about:blank#blocked). Text fields
// keep their own drop handling.
function preventNavigatingDrop(event: DragEvent): void {
  const { target } = event;
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement
  ) {
    return;
  }
  const types = event.dataTransfer?.types ?? [];
  if (NAVIGATING_DRAG_TYPES.some((type) => types.includes(type))) {
    event.preventDefault();
  }
}

function readAsDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function attachImage(file: File): Promise<Attachment | undefined> {
  try {
    const image = await downscaleImage(file);
    if (image.size > MAX_IMAGE_BYTES) {
      toast.error(`${file.name} is larger than 5 MB even after resizing.`);
      return undefined;
    }
    return {
      url: await readAsDataUrl(image),
      name: file.name,
      contentType: file.type,
    };
  } catch (_error) {
    toast.error(`Failed to read ${file.name}.`);
    return undefined;
  }
}

function PureMultimodalInput({
  chatId,
  input,
  setInput,
  status,
  stop,
  attachments,
  setAttachments,
  messages,
  setMessages,
  sendMessage,
  selectedVisibilityType,
}: {
  chatId: string;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  status: UseChatHelpers<ChatMessage>['status'];
  stop: () => void;
  attachments: Array<Attachment>;
  setAttachments: Dispatch<SetStateAction<Array<Attachment>>>;
  messages: Array<ChatMessage>;
  setMessages: UseChatHelpers<ChatMessage>['setMessages'];
  sendMessage: UseChatHelpers<ChatMessage>['sendMessage'];
  selectedVisibilityType: VisibilityType;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { width } = useWindowSize();
  const {
    chatHistoryEnabled,
    fileUploadEnabled,
    imageInputEnabled,
    speechInputEnabled,
  } = useAppConfig();

  const [localStorageInput, setLocalStorageInput] = useLocalStorage(
    'input',
    '',
  );

  useEffect(() => {
    if (textareaRef.current) {
      const domValue = textareaRef.current.value;
      // Prefer DOM value over localStorage to handle hydration
      const finalValue = domValue || localStorageInput || '';
      setInput(finalValue);
    }
  }, [localStorageInput, setInput]);

  useEffect(() => {
    setLocalStorageInput(input);
  }, [input, setLocalStorageInput]);

  const handleInput = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(event.target.value);
  };

  // Speech input: the transcript lands at the cursor for review, never sent.
  const handleTranscript = useCallback(
    (transcript: string) => {
      const textarea = textareaRef.current;
      const start = textarea?.selectionStart ?? Number.MAX_SAFE_INTEGER;
      const end = textarea?.selectionEnd ?? start;
      setInput((current) => insertTranscript(current, transcript, start, end).text);
      textarea?.focus();
    },
    [setInput],
  );

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadQueue, setUploadQueue] = useState<Array<string>>([]);
  const [isDragging, setIsDragging] = useState(false);

  const submitForm = useCallback(() => {
    softNavigateToChatId(chatId, chatHistoryEnabled);

    const fileParts = attachments.map((attachment) => ({
      type: 'file' as const,
      url: attachment.url,
      name: attachment.name,
      filename: attachment.name,
      mediaType: attachment.contentType,
      ...(attachment.extractedText !== undefined
        ? {
            extractedText: attachment.extractedText,
            truncated: attachment.truncated,
            format: attachment.format,
          }
        : {}),
    }));

    // Omit the text part entirely when empty: the request schema requires a
    // non-empty text part, so a file-only message must not append one.
    const textParts = input.trim()
      ? [{ type: 'text' as const, text: input }]
      : [];

    sendMessage({
      role: 'user',
      parts: [...fileParts, ...textParts],
    });

    setAttachments([]);
    setLocalStorageInput('');
    setInput('');

    if (width && width > 768) {
      textareaRef.current?.focus();
    }
  }, [
    input,
    setInput,
    attachments,
    sendMessage,
    setAttachments,
    setLocalStorageInput,
    width,
    chatId,
    chatHistoryEnabled,
  ]);

  const uploadFile = useCallback(
    async (file: File): Promise<Attachment | undefined> => {
      const formData = new FormData();
      formData.append('file', file);

      try {
        const response = await fetch('/api/files/upload', {
          method: 'POST',
          body: formData,
        });

        if (response.ok) {
          const data = await response.json();
          return {
            // Documents have no fetchable URL; a synthetic id keeps React keys
            // and remove-by-url filtering unique.
            url: `upload://${crypto.randomUUID()}`,
            name: data.name,
            contentType: file.type || 'text/plain',
            extractedText: data.extractedText,
            truncated: data.truncated,
            format: data.format,
          };
        }

        const { error } = await response
          .json()
          .catch(() => ({ error: 'Failed to upload file.' }));
        toast.error(error || 'Failed to upload file.');
      } catch (_error) {
        toast.error('Failed to upload file, please try again!');
      }
      return undefined;
    },
    [],
  );

  const attachFile = useCallback(
    (file: File): Promise<Attachment | undefined> =>
      imageInputEnabled && IMAGE_MEDIA_TYPES.has(file.type)
        ? attachImage(file)
        : uploadFile(file),
    [imageInputEnabled, uploadFile],
  );

  const processFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;

      setUploadQueue(files.map((file) => file.name));

      try {
        const uploaded = await Promise.all(files.map((file) => attachFile(file)));
        const successful = uploaded.filter(
          (attachment): attachment is Attachment => attachment !== undefined,
        );

        setAttachments((currentAttachments) => [
          ...currentAttachments,
          ...successful,
        ]);
      } catch (error) {
        console.error('Error uploading files!', error);
      } finally {
        setUploadQueue([]);
      }
    },
    [setAttachments, attachFile],
  );

  const handleFileChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || []);
      await processFiles(files);
      // Allow re-selecting the same file after removal.
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    },
    [processFiles],
  );

  useEffect(() => {
    if (!fileUploadEnabled) return;
    window.addEventListener('dragover', preventNavigatingDrop);
    window.addEventListener('drop', preventNavigatingDrop);
    return () => {
      window.removeEventListener('dragover', preventNavigatingDrop);
      window.removeEventListener('drop', preventNavigatingDrop);
    };
  }, [fileUploadEnabled]);

  const handleDragOver = useCallback(
    (event: React.DragEvent) => {
      if (!fileUploadEnabled) return;
      event.preventDefault();
      setIsDragging(true);
    },
    [fileUploadEnabled],
  );

  const handleDragLeave = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      if (!fileUploadEnabled) return;
      event.preventDefault();
      setIsDragging(false);
      const files = Array.from(event.dataTransfer?.files || []);
      void processFiles(files);
    },
    [fileUploadEnabled, processFiles],
  );

  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      if (!fileUploadEnabled) return;
      const files = Array.from(event.clipboardData?.files || []);
      if (files.length > 0) {
        event.preventDefault();
        void processFiles(files);
      }
    },
    [fileUploadEnabled, processFiles],
  );

  const isBusy = status === 'submitted' || status === 'streaming';

  return (
    <div className="relative flex w-full flex-col gap-4">
      <input
        type="file"
        accept={
          imageInputEnabled
            ? `${SUPPORTED_FILE_ACCEPT},${IMAGE_FILE_ACCEPT}`
            : SUPPORTED_FILE_ACCEPT
        }
        className="-top-4 -left-4 pointer-events-none fixed size-0.5 opacity-0"
        ref={fileInputRef}
        multiple
        onChange={handleFileChange}
        tabIndex={-1}
      />

      <div className="w-full rounded-[24px] border-4 border-black/[0.02] dark:border-white/[0.02]">
        <PromptInput
          className={`rounded-[24px] border bg-background/75 p-4 backdrop-blur-sm transition-all duration-200 ${
            isDragging
              ? 'border-primary ring-2 ring-primary'
              : 'border-black/[0.08] dark:border-white/[0.08]'
          }`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onSubmit={(event) => {
            event.preventDefault();
            if (isBusy) {
              toast.error('Please wait for the model to finish its response!');
            } else {
              submitForm();
            }
          }}
        >
          {(attachments.length > 0 || uploadQueue.length > 0) && (
            <div
              data-testid="attachments-preview"
              className="flex flex-row items-end gap-2 overflow-x-scroll"
            >
              {attachments.map((attachment) => (
                <PreviewAttachment
                  key={attachment.url}
                  attachment={attachment}
                  onRemove={() => {
                    setAttachments((currentAttachments) =>
                      currentAttachments.filter((a) => a.url !== attachment.url),
                    );
                    if (fileInputRef.current) {
                      fileInputRef.current.value = '';
                    }
                  }}
                />
              ))}

              {uploadQueue.map((filename) => (
                <PreviewAttachment
                  key={filename}
                  attachment={{
                    url: '',
                    name: filename,
                    contentType: '',
                  }}
                  isUploading={true}
                />
              ))}
            </div>
          )}
          <div className="flex flex-row items-start gap-1 sm:gap-2">
            <PromptInputTextarea
              data-testid="multimodal-input"
              ref={textareaRef}
              placeholder="Ask a question..."
              value={input}
              onChange={handleInput}
              onPaste={handlePaste}
              className="min-h-11 max-h-[200px] grow resize-none border-0! border-none! bg-transparent p-2 text-sm outline-none ring-0 [-ms-overflow-style:none] [scrollbar-width:none] placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 [&::-webkit-scrollbar]:hidden text-base md:text-base"
              rows={1}
              autoFocus
            />{' '}
          </div>
          <PromptInputToolbar className="!border-top-0 border-t-0! p-0 shadow-none dark:border-0 dark:border-transparent!">
            <PromptInputTools className="gap-0 sm:gap-0.5">
              {fileUploadEnabled && (
                <Button
                  data-testid="attach-button"
                  type="button"
                  variant="tertiary"
                  size="icon"
                  className="size-8 rounded-full text-muted-foreground transition-colors duration-200 hover:text-foreground disabled:opacity-50"
                  disabled={isBusy}
                  onClick={() => fileInputRef.current?.click()}
                  aria-label="Attach file"
                >
                  <Paperclip className="size-4" />
                </Button>
              )}
            </PromptInputTools>

            <div className="flex items-center gap-1">
            <ContextMeter chatId={chatId} messages={messages} />
            {speechInputEnabled && (
              <MicButton disabled={isBusy} onTranscript={handleTranscript} />
            )}
            {isBusy ? (
              <StopButton stop={stop} setMessages={setMessages} />
            ) : (
              <PromptInputSubmit
                data-testid="send-button"
                status={status}
                variant="tertiary"
                disabled={
                  (!input.trim() && attachments.length === 0) ||
                  uploadQueue.length > 0
                }
                className="size-8 rounded-full bg-secondary hover:bg-action-tertiary-background-hover text-foreground transition-colors duration-200 disabled:opacity-50 disabled:text-muted-foreground"
              >
                <DbIcon icon={ArrowUpIcon} className="size-4" />
              </PromptInputSubmit>
            )}
            </div>
          </PromptInputToolbar>
        </PromptInput>
      </div>

      <p className="-mt-2 text-center text-sm text-muted-foreground">
        Always review the accuracy of responses.
      </p>

      {messages.length === 0 &&
        attachments.length === 0 &&
        uploadQueue.length === 0 && (
          <SuggestedActions
            sendMessage={sendMessage}
            setMessages={setMessages}
            chatId={chatId}
            selectedVisibilityType={selectedVisibilityType}
          />
        )}
    </div>
  );
}

export const MultimodalInput = memo(
  PureMultimodalInput,
  (prevProps, nextProps) => {
    if (prevProps.input !== nextProps.input) return false;
    if (prevProps.status !== nextProps.status) return false;
    if (!equal(prevProps.attachments, nextProps.attachments)) return false;
    if (prevProps.selectedVisibilityType !== nextProps.selectedVisibilityType)
      return false;

    return true;
  },
);

function PureStopButton({
  stop,
  setMessages,
}: {
  stop: () => void;
  setMessages: UseChatHelpers<ChatMessage>['setMessages'];
}) {
  return (
    <Button
      data-testid="stop-button"
      className="size-7 rounded-full bg-foreground p-1 text-background transition-colors duration-200 hover:bg-foreground/90 disabled:bg-muted disabled:text-muted-foreground"
      onClick={(event) => {
        event.preventDefault();
        stop();
        setMessages((messages) => messages);
      }}
    >
      <DbIcon icon={StopIcon} className="size-4" />
    </Button>
  );
}

const StopButton = memo(PureStopButton);
