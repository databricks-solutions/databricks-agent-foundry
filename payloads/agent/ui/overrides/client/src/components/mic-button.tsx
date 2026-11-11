import { Loader2, Mic, Square, X } from 'lucide-react';
import { Button } from './ui/button';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { useSpeechRecorder } from '@/hooks/use-speech-recorder';
import { formatElapsed } from '@/lib/speech-recorder';

// Agent Bricks speech input (docs/explanation/speech-to-text-design.md).
// Rendered only while `speechInputEnabled`; the transcript goes to the
// composer through `onTranscript` and is never sent automatically.

const ICON_BUTTON =
  'size-8 rounded-full text-muted-foreground transition-colors duration-200 hover:text-foreground disabled:opacity-50';

export function MicButton({
  disabled,
  onTranscript,
}: {
  disabled: boolean;
  onTranscript: (text: string) => void;
}) {
  const { speechMaxSeconds, speechLanguage } = useAppConfig();
  const { state, elapsedSeconds, start, stop, cancel } = useSpeechRecorder({
    maxSeconds: speechMaxSeconds,
    language: speechLanguage,
    onTranscript,
  });

  if (state === 'transcribing') {
    return (
      <Button
        data-testid="mic-button"
        type="button"
        variant="tertiary"
        size="icon"
        className={ICON_BUTTON}
        onClick={cancel}
        aria-label="Cancel transcription"
        title="Transcribing… click to cancel"
      >
        <Loader2 className="size-4 animate-spin" />
      </Button>
    );
  }

  if (state === 'recording') {
    return (
      <div className="flex items-center gap-1" data-testid="mic-recording">
        <span className="tabular-nums text-xs text-red-600" aria-live="polite">
          {formatElapsed(elapsedSeconds)} / {formatElapsed(speechMaxSeconds)}
        </span>
        <Button
          type="button"
          variant="tertiary"
          size="icon"
          className={ICON_BUTTON}
          onClick={cancel}
          aria-label="Discard recording"
        >
          <X className="size-4" />
        </Button>
        <Button
          data-testid="mic-button"
          type="button"
          variant="tertiary"
          size="icon"
          className={`${ICON_BUTTON} text-red-600 hover:text-red-700`}
          onClick={stop}
          aria-label="Stop recording and transcribe"
        >
          <Square className="size-3.5 animate-pulse fill-current" />
        </Button>
      </div>
    );
  }

  return (
    <Button
      data-testid="mic-button"
      type="button"
      variant="tertiary"
      size="icon"
      className={ICON_BUTTON}
      disabled={disabled}
      onClick={() => void start()}
      aria-label="Record voice input"
      title="Record voice input"
    >
      <Mic className="size-4" />
    </Button>
  );
}
