import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  AUDIO_BITS_PER_SECOND,
  heardSpeech,
  isRetryableStatus,
  languageHint,
  pickRecordingMimeType,
  rmsLevel,
  transcriptionErrorMessage,
} from '@/lib/speech-recorder';

export type SpeechRecorderState = 'idle' | 'recording' | 'transcribing';

interface SpeechRecorderOptions {
  maxSeconds: number;
  /** Instance language hint; empty falls back to the browser language. */
  language: string;
  onTranscript: (text: string) => void;
}

export interface SpeechRecorder {
  state: SpeechRecorderState;
  elapsedSeconds: number;
  start: () => Promise<void>;
  stop: () => void;
  cancel: () => void;
}

/** One recording: the recorder, its microphone stream and the level monitor. */
interface Session {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  startedAt: number;
  audioContext: AudioContext | null;
  levelTimer: ReturnType<typeof setInterval> | undefined;
  peakLevel: number;
  cancelled: boolean;
}

type UploadOutcome = { ok: true; text: string } | { ok: false; status: number; body: unknown };

const LEVEL_POLL_MS = 100;
const TICK_MS = 250;
/** Long enough to decide on a retry; the recording lives only as long as the toast. */
const RETRY_TOAST_MS = 30_000;

function newSession(stream: MediaStream, mimeType: string): Session {
  return {
    recorder: new MediaRecorder(stream, { mimeType, audioBitsPerSecond: AUDIO_BITS_PER_SECOND }),
    stream,
    chunks: [],
    startedAt: Date.now(),
    audioContext: null,
    levelTimer: undefined,
    peakLevel: 0,
    cancelled: false,
  };
}

function sampleLevel(session: Session, analyser: AnalyserNode, samples: Float32Array): void {
  analyser.getFloatTimeDomainData(samples);
  session.peakLevel = Math.max(session.peakLevel, rmsLevel(samples));
}

/** Tracks the loudest input level so silent recordings are never uploaded. */
function monitorLevel(session: Session): void {
  try {
    const audioContext = new AudioContext();
    const analyser = audioContext.createAnalyser();
    audioContext.createMediaStreamSource(session.stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    session.audioContext = audioContext;
    session.levelTimer = setInterval(() => sampleLevel(session, analyser, samples), LEVEL_POLL_MS);
  } catch {
    // Without Web Audio there is no silence check; the backend still rejects empty audio.
    session.peakLevel = Number.POSITIVE_INFINITY;
  }
}

/** Stops every microphone track, or the browser keeps showing the mic as in use. */
function release(session: Session): void {
  clearInterval(session.levelTimer);
  void session.audioContext?.close().catch(() => undefined);
  for (const track of session.stream.getTracks()) {
    track.stop();
  }
}

async function postRecording(
  audio: Blob,
  hint: string | undefined,
  signal: AbortSignal,
): Promise<UploadOutcome> {
  const headers: Record<string, string> = { 'Content-Type': audio.type };
  if (hint) {
    headers['X-Speech-Language'] = hint;
  }
  const response = await fetch('/api/transcribe', {
    method: 'POST',
    headers,
    body: audio,
    signal,
    credentials: 'same-origin',
  });
  const body: unknown = await response.json().catch(() => null);
  const text = (body as { text?: unknown } | null)?.text;
  if (!response.ok || typeof text !== 'string') {
    return { ok: false, status: response.status, body };
  }
  return { ok: true, text };
}

/** The error toast, with a retry of the same recording when it may succeed later. */
function showTranscriptionError(status: number, body: unknown, retry: () => void): void {
  const message = transcriptionErrorMessage(status, body);
  if (!isRetryableStatus(status)) {
    toast.error(message);
    return;
  }
  toast.error(message, { duration: RETRY_TOAST_MS, action: { label: 'Retry', onClick: retry } });
}

async function openMicrophone(): Promise<MediaStream | null> {
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast.error('Microphone access was denied or no microphone is available.');
    return null;
  }
}

function supportedMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices) {
    return undefined;
  }
  return pickRecordingMimeType((mimeType) => MediaRecorder.isTypeSupported(mimeType));
}

/** Records from the microphone and transcribes through `POST /api/transcribe`. */
export function useSpeechRecorder({
  maxSeconds,
  language,
  onTranscript,
}: SpeechRecorderOptions): SpeechRecorder {
  const [state, setState] = useState<SpeechRecorderState>('idle');
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const sessionRef = useRef<Session | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const transcribeRef = useRef<(session: Session) => Promise<void>>(async () => undefined);

  /** Re-sends a failed recording unless another one is recording or transcribing. */
  const retry = useCallback((session: Session) => {
    if (!sessionRef.current && !abortRef.current) {
      void transcribeRef.current(session);
    }
  }, []);

  const transcribe = useCallback(
    async (session: Session) => {
      if (session.cancelled) {
        setState('idle');
        return;
      }
      if (!heardSpeech(session.peakLevel)) {
        toast.error('No speech was detected in the recording.');
        setState('idle');
        return;
      }
      const controller = new AbortController();
      abortRef.current = controller;
      setState('transcribing');
      try {
        const audio = new Blob(session.chunks, { type: session.recorder.mimeType });
        const outcome = await postRecording(audio, languageHint(language, navigator.language), controller.signal);
        if (!outcome.ok) {
          showTranscriptionError(outcome.status, outcome.body, () => retry(session));
        } else if (outcome.text) {
          onTranscriptRef.current(outcome.text);
        }
      } catch {
        if (!controller.signal.aborted) {
          showTranscriptionError(0, null, () => retry(session));
        }
      } finally {
        abortRef.current = null;
        setState('idle');
      }
    },
    [language, retry],
  );
  transcribeRef.current = transcribe;

  const finish = useCallback(
    (session: Session) => {
      release(session);
      sessionRef.current = null;
      void transcribe(session);
    },
    [transcribe],
  );

  const start = useCallback(async () => {
    if (sessionRef.current || abortRef.current) {
      return;
    }
    const mimeType = supportedMimeType();
    if (!mimeType) {
      toast.error('This browser cannot record audio.');
      return;
    }
    const stream = await openMicrophone();
    if (!stream) {
      return;
    }
    const session = newSession(stream, mimeType);
    session.recorder.ondataavailable = (event) => session.chunks.push(event.data);
    session.recorder.onstop = () => finish(session);
    monitorLevel(session);
    sessionRef.current = session;
    session.recorder.start();
    setElapsedSeconds(0);
    setState('recording');
  }, [finish]);

  const stop = useCallback(() => {
    const recorder = sessionRef.current?.recorder;
    if (recorder?.state === 'recording') {
      recorder.stop();
    }
  }, []);

  const cancel = useCallback(() => {
    if (sessionRef.current) {
      sessionRef.current.cancelled = true;
      stop();
    }
    abortRef.current?.abort();
  }, [stop]);

  const tick = useCallback(() => {
    const session = sessionRef.current;
    if (!session) {
      return;
    }
    const elapsed = (Date.now() - session.startedAt) / 1000;
    setElapsedSeconds(Math.floor(elapsed));
    if (elapsed >= maxSeconds) {
      stop();
    }
  }, [maxSeconds, stop]);

  useEffect(() => {
    if (state !== 'recording') {
      return undefined;
    }
    const timer = setInterval(tick, TICK_MS);
    return () => clearInterval(timer);
  }, [state, tick]);

  useEffect(() => cancel, [cancel]);

  return { state, elapsedSeconds, start, stop, cancel };
}
