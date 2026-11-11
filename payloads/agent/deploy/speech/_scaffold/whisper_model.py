"""Whisper large-v3-turbo (faster-whisper) as a typed MLflow pyfunc for the speech input.

Logged as models-from-code by ``build_whisper.py``; the CTranslate2 weights
ship as the ``whisper_dir`` artifact. Request and response shapes are the
``serving_invocations`` contract of the chat UI's transcribe route. Never
logs audio or transcripts.
"""

from __future__ import annotations

import base64
import ctypes
import importlib
import io
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mlflow
from mlflow.pyfunc.model import PythonModel, PythonModelContext
from pydantic import BaseModel

WEIGHTS_ARTIFACT = "whisper_dir"
BATCH_SIZE = 8
CUDA_LIB_PACKAGES = ("nvidia.cublas", "nvidia.cudnn")
PRELOAD_PASSES = 5
SAMPLING_RATE = 16000
# The contract's max_seconds ceiling (300) plus the route's grace; enforced before inference.
MAX_AUDIO_SECONDS = 302


class TranscriptionRequest(BaseModel):
    audio_base64: str
    mime_type: str
    language: str | None = None


class TranscriptionResponse(BaseModel):
    text: str
    language: str
    duration_seconds: float


def cuda_library_files() -> list[Path]:
    """Shared libraries shipped by the pip packages ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12``."""
    files: list[Path] = []
    for package in CUDA_LIB_PACKAGES:
        try:
            module = importlib.import_module(package)
        except ImportError:
            continue
        for root in module.__path__:
            files.extend(sorted((Path(root) / "lib").glob("lib*.so*")))
    return files


def preload_cuda_libraries() -> list[str]:
    """Load cuBLAS and cuDNN globally so CTranslate2 finds them without ``LD_LIBRARY_PATH``.

    The libraries depend on each other, so files that fail are retried after the rest loaded.
    """
    pending = cuda_library_files()
    loaded: list[str] = []
    for _ in range(PRELOAD_PASSES):
        failed: list[Path] = []
        for path in pending:
            try:
                ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
                loaded.append(path.name)
            except OSError:
                failed.append(path)
        if not failed or len(failed) == len(pending):
            break
        pending = failed
    return loaded


class WhisperModel(PythonModel):
    _pipeline: Any
    _decode_audio: Callable[..., Any]

    def load_context(self, context: PythonModelContext) -> None:
        if platform.system() == "Linux":
            # The endpoint logs show the serving worker's stderr, not its stdout.
            print(f"preloaded CUDA libraries: {preload_cuda_libraries()}", file=sys.stderr, flush=True)

        import ctranslate2
        from faster_whisper import BatchedInferencePipeline, decode_audio
        from faster_whisper import WhisperModel as FasterWhisper

        self._decode_audio = decode_audio

        use_cuda = ctranslate2.get_cuda_device_count() > 0
        device, compute_type = ("cuda", "float16") if use_cuda else ("cpu", "int8")
        print(f"ctranslate2 {ctranslate2.__version__} on {device} ({compute_type})", file=sys.stderr, flush=True)
        model = FasterWhisper(context.artifacts[WEIGHTS_ARTIFACT], device=device, compute_type=compute_type)
        self._pipeline = BatchedInferencePipeline(model)

    def predict(
        self,
        context: PythonModelContext,
        model_input: list[TranscriptionRequest],
        params: dict[str, Any] | None = None,
    ) -> list[TranscriptionResponse]:
        return [self._transcribe(request) for request in model_input]

    def _transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        encoded = io.BytesIO(base64.b64decode(request.audio_base64, validate=True))
        audio = self._decode_audio(encoded, sampling_rate=SAMPLING_RATE)
        if len(audio) > MAX_AUDIO_SECONDS * SAMPLING_RATE:
            raise ValueError(f"Audio is longer than {MAX_AUDIO_SECONDS} seconds")
        segments, info = self._pipeline.transcribe(
            audio,
            language=request.language,
            batch_size=BATCH_SIZE,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return TranscriptionResponse(text=text, language=info.language, duration_seconds=round(info.duration, 2))


mlflow.models.set_model(WhisperModel())
