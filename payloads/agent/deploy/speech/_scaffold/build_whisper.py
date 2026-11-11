"""Build the speech model: download Whisper turbo, log ``whisper_model.py``, smoke test, register.

Run by the Model Serving brick as a serverless job with
``--model-name <catalog.schema.model> --experiment <workspace path>``.
"""

from __future__ import annotations

import argparse
import base64
import importlib.metadata
import io
import tempfile
import wave
from pathlib import Path
from typing import Any

import mlflow

WEIGHTS_REPO = "dropbox-dash/faster-whisper-large-v3-turbo"
WEIGHTS_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"


def _script_dir() -> Path:
    # Serverless runs the script through exec(compile(...)), which leaves __file__ unset.
    return Path(_script_dir.__code__.co_filename).resolve().parent


MODEL_CODE = _script_dir() / "whisper_model.py"
PINNED_PACKAGES = (
    "mlflow",
    "pydantic",
    "faster-whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "onnxruntime",
    "huggingface-hub",
    "numpy",
)
CUDA_REQUIREMENTS = ("nvidia-cublas-cu12==12.*", "nvidia-cudnn-cu12==9.*")


def pinned_requirements() -> list[str]:
    """Serving requirements pinned to what this build ran with, plus the CUDA runtime."""
    pins = [f"{name}=={importlib.metadata.version(name)}" for name in PINNED_PACKAGES]
    return pins + list(CUDA_REQUIREMENTS)


def silent_wav_base64(seconds: float = 2.0, rate: int = 16000) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _prediction_text(prediction: Any) -> str:
    return str(prediction["text"] if isinstance(prediction, dict) else prediction.text)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args(argv)
    from huggingface_hub import snapshot_download

    weights_dir = snapshot_download(
        WEIGHTS_REPO, revision=WEIGHTS_REVISION, local_dir=tempfile.mkdtemp(prefix="whisper-")
    )
    mlflow.set_registry_uri("databricks-uc")
    mlflow.set_experiment(args.experiment)
    with mlflow.start_run(run_name="build-whisper"):
        model_info = mlflow.pyfunc.log_model(
            name="whisper",
            python_model=str(MODEL_CODE),
            artifacts={"whisper_dir": weights_dir},
            pip_requirements=pinned_requirements(),
        )

    loaded = mlflow.pyfunc.load_model(model_info.model_uri)
    smoke = loaded.predict([{"audio_base64": silent_wav_base64(), "mime_type": "audio/wav", "language": None}])
    if _prediction_text(smoke[0]) != "":
        raise RuntimeError("Smoke test failed: silence must transcribe to empty text")

    version = mlflow.register_model(model_info.model_uri, args.model_name)
    print(f"Registered {args.model_name} version {version.version}")


if __name__ == "__main__":
    # No SystemExit: serverless runs the script under IPython, which fails the task on any SystemExit, even 0.
    main()
