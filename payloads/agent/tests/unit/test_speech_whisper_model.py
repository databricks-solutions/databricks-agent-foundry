"""The speech scaffold: Whisper pyfunc with a stub engine, and its endpoint config."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agent_foundry.scaffold import run_scaffold
from bricks.model_serving.scripts.deploy_endpoint import load_endpoint_config

SCAFFOLD = Path(__file__).resolve().parents[2] / "deploy" / "speech" / "_scaffold"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"speech_scaffold_{name}", SCAFFOLD / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubPipeline:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Any], Any]:
        self.calls.append({"audio": audio, **kwargs})
        segments = [SimpleNamespace(text=text) for text in self.texts]
        return segments, SimpleNamespace(language="de", duration=3.14159)


class _StubDecoder:
    """Stands in for ``faster_whisper.decode_audio``: one sample per second of ``seconds``."""

    def __init__(self, seconds: int = 3) -> None:
        self.seconds = seconds
        self.encoded: list[bytes] = []

    def __call__(self, file: Any, sampling_rate: int) -> list[float]:
        self.encoded.append(file.read())
        return [0.0] * (self.seconds * sampling_rate)


@pytest.fixture(scope="module")
def whisper_model() -> ModuleType:
    return _load("whisper_model")


def _model(module: ModuleType, pipeline: _StubPipeline, decoder: _StubDecoder | None = None) -> Any:
    model = module.WhisperModel()
    model._pipeline = pipeline
    model._decode_audio = decoder or _StubDecoder()
    return model


def _request(module: ModuleType, audio: bytes, language: str | None = None) -> Any:
    encoded = base64.b64encode(audio).decode("ascii")
    return module.TranscriptionRequest(audio_base64=encoded, mime_type="audio/webm", language=language)


def test_predict_joins_segments_and_reports_language(whisper_model: ModuleType) -> None:
    pipeline = _StubPipeline([" Hallo ", "Welt. "])
    decoder = _StubDecoder()
    model = _model(whisper_model, pipeline, decoder)
    [response] = model.predict(None, [_request(whisper_model, b"\x01\x02", "de")])

    assert (response.text, response.language, response.duration_seconds) == ("Hallo Welt.", "de", 3.14)
    assert decoder.encoded == [b"\x01\x02"]
    call = pipeline.calls[0]
    assert len(call["audio"]) == 3 * whisper_model.SAMPLING_RATE
    assert (call["language"], call["vad_filter"], call["condition_on_previous_text"]) == ("de", True, False)


def test_audio_over_the_limit_is_rejected_before_inference(whisper_model: ModuleType) -> None:
    pipeline = _StubPipeline(["never"])
    decoder = _StubDecoder(seconds=whisper_model.MAX_AUDIO_SECONDS + 1)
    with pytest.raises(ValueError, match="longer than"):
        _model(whisper_model, pipeline, decoder).predict(None, [_request(whisper_model, b"\x01")])
    assert pipeline.calls == []


def test_silence_is_empty_text(whisper_model: ModuleType) -> None:
    [response] = _model(whisper_model, _StubPipeline([])).predict(None, [_request(whisper_model, b"\x00")])
    assert response.text == ""


def test_invalid_base64_is_rejected(whisper_model: ModuleType) -> None:
    request = whisper_model.TranscriptionRequest(audio_base64="not base64!", mime_type="audio/webm")
    with pytest.raises(ValueError):
        _model(whisper_model, _StubPipeline([])).predict(None, [request])


def test_preload_retries_libraries_that_need_another_first(
    whisper_model: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [Path("libcudnn.so.9"), Path("libcublas.so.12")]
    monkeypatch.setattr(whisper_model, "cuda_library_files", lambda: files)
    loaded_names: set[str] = set()

    def fake_cdll(path: str, mode: int) -> None:
        del mode
        if path == "libcudnn.so.9" and "libcublas.so.12" not in loaded_names:
            raise OSError("needs cublas")
        loaded_names.add(path)

    monkeypatch.setattr(whisper_model.ctypes, "CDLL", fake_cdll)
    assert whisper_model.preload_cuda_libraries() == ["libcublas.so.12", "libcudnn.so.9"]


def test_build_script_pins_what_it_ran_with_plus_the_cuda_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    build = _load("build_whisper")
    monkeypatch.setattr(build.importlib.metadata, "version", lambda name: "1.0")
    requirements = build.pinned_requirements()
    assert "faster-whisper==1.0" in requirements
    assert "nvidia-cudnn-cu12==9.*" in requirements


def test_build_script_pins_the_weights_to_a_commit() -> None:
    build = _load("build_whisper")
    assert len(build.WEIGHTS_REVISION) == 40
    assert int(build.WEIGHTS_REVISION, 16) >= 0


def test_scaffold_is_a_valid_gpu_build_endpoint(tmp_path: Path) -> None:
    instance = tmp_path / "instances" / "demo"
    run_scaffold("speech_input", instance)
    config = load_endpoint_config(
        instance / "model_serving" / "speech", {"app_name": "demo", "catalog": "c", "schema_prefix": "demo"}
    )
    assert config.endpoint_name == "demo-speech"
    assert config.model.name == "c.demo_agent.speech_whisper_turbo"
    assert config.model.workload_type == "GPU_SMALL"
    assert config.build is not None
    assert config.build.files == ["./whisper_model.py"]
    assert "av>=11,<19" in config.build.dependencies
    assert (instance / "model_serving" / "speech" / "whisper_model.py").is_file()
