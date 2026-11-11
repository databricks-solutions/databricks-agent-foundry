"""
Bundled Agent + Chat UI Process Manager
=========================================
Starts the Python agent backend and the Node.js ``e2e-chatbot-app-next``
chat frontend as child processes inside a single Databricks App.

The agent backend (``start_server.py``) listens on ``--port`` (default 8000)
and the chat UI listens on ``CHAT_APP_PORT`` (default 3000).  The AgentServer
with ``enable_chat_proxy=True`` proxies the frontend, so external traffic
only needs to reach port 8000.

On first run the chat UI is sparse-cloned from
``databricks/app-templates`` on GitHub.

Adapted from the official Databricks agent template:
https://github.com/databricks/app-templates/blob/main/agent-openai-agents-sdk/scripts/start_app.py

Usage::

    python bricks/app/scripts/start_app.py
    python bricks/app/scripts/start_app.py --no-ui
    python bricks/app/scripts/start_app.py --port 8001
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from importlib import resources as _resources
from pathlib import Path
from typing import IO

from agent_foundry import env as _env

# PROJECT_ROOT is the dir where the chat UI is cloned and where
# ``backend.log`` / ``frontend.log`` are written.  Resolution is now
# explicit (issue #143) -- driven by the ``AGENT_FOUNDRY_PROJECT_ROOT``
# env var set by ``app.yaml`` (deployed App) or ``agent-foundry
# run``/``repl`` (local dev). Defaults to CWD when unset (direct
# ``python -m bricks.app.scripts.start_app`` invocations).  We
# also export the resolved path back into the environment so the
# spawned ``start_server`` subprocess sees a deterministic value.
PROJECT_ROOT = Path(_env.read_optional(_env.AGENT_FOUNDRY_PROJECT_ROOT) or ".").resolve()
os.environ.setdefault(_env.AGENT_FOUNDRY_PROJECT_ROOT.name, str(PROJECT_ROOT))

BACKEND_READY_PATTERNS = [
    r"Uvicorn running on",
    r"Application startup complete",
    r"Started server process",
]
FRONTEND_READY_PATTERNS = [
    r"Server is running on http://localhost",
]

CHAT_UI_DIR_NAME = "e2e-chatbot-app-next"

# Bounded restart policy for the isolated extractor (§7): respawn a
# crashed worker with a fixed backoff, capped so a crash-loop cannot
# busy-spin.  Tuning lives here rather than in env to keep the contract
# small; the extractor's exit never fails the app.
_EXTRACTOR_MAX_RESTARTS = 5
_EXTRACTOR_RESTART_BACKOFF_SECONDS = 2

# Chat UI build steps, replacing upstream's single ``npm run build``
# (which chains ``db:migrate && build:client && build:server``).  Run
# explicitly so the Lakebase migration is *non-fatal* -- see
# ``ProcessManager._build_chat_ui``.  Each entry is ``(command, fatal)``.
_CHAT_UI_BUILD_STEPS: tuple[tuple[str, bool], ...] = (
    ("npm install", True),
    ("npm run db:migrate", False),
    ("npm run build:client", True),
    ("npm run build:server", True),
)

# OVERRIDES_DIR points at the ``payloads/agent/ui/overrides/`` tree
# shipped as package data inside the wheel (#112 + the explicit
# ``[tool.setuptools.package-data]`` block in ``pyproject.toml``).
# The agent chat UI moved under the ``agent`` payload's ``ui/``
# container (issue #338) so future payloads can ship their own UIs.
# ``importlib.resources.files`` returns a Traversable; for a regular
# package with ``__init__.py`` (which ``payloads.agent`` is), the
# path resolves to a real directory on disk that ``rglob`` can walk.
OVERRIDES_DIR = Path(str(_resources.files("payloads.agent").joinpath("ui", "overrides")))

UPSTREAM_TEMPLATE_SHA = "861de21f0dcd90572293fd883d51f863dce94e05"


def _extra_overlay_dirs() -> list[Path]:
    """``ui/overrides/`` of each package in ``CHAT_UI_OVERLAYS``, in order."""
    raw = _env.read_optional(_env.CHAT_UI_OVERLAYS) or ""
    packages = [name.strip() for name in raw.split(",") if name.strip()]
    return [Path(str(_resources.files(package).joinpath("ui", "overrides"))) for package in packages]


def _check_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", port))
            return True
    except OSError:
        return False


class ProcessManager:
    def __init__(self, port: int = 8000, no_ui: bool = False) -> None:
        self.backend_process: subprocess.Popen[str] | None = None
        self.frontend_process: subprocess.Popen[str] | None = None
        # Isolated file-extraction service (D6); started only when
        # ENABLE_FILE_UPLOAD is truthy.  Its lifecycle is deliberately
        # decoupled from app failure -- see ``_monitor_extractor``.
        self.extractor_process: subprocess.Popen[str] | None = None
        # Set during cleanup so the extractor supervisor stops respawning.
        self.shutting_down = False
        self.backend_ready = False
        self.frontend_ready = False
        self.failed = threading.Event()
        # Opened lazily in ``run()`` once we know which logs we need;
        # type as ``IO[str] | None`` so the open() assignments below
        # type-check.
        self.backend_log: IO[str] | None = None
        self.frontend_log: IO[str] | None = None
        self.extractor_log: IO[str] | None = None
        self.port = port
        self.no_ui = no_ui

    def check_ports(self) -> None:
        errors: list[str] = []
        if not _check_port_available(self.port):
            errors.append(
                f"Port {self.port} (backend) is already in use.\n  To free it: lsof -ti :{self.port} | xargs kill -9"
            )

        if not self.no_ui:
            frontend_port = _env.read_int(_env.CHAT_APP_PORT) or _env.read_int(_env.PORT, fallback=3000) or 3000
            if self.port == frontend_port:
                print(f"ERROR: Backend and frontend are both configured to use port {self.port}.")
                print("  Set CHAT_APP_PORT to a different port (e.g., CHAT_APP_PORT=3000).")
                sys.exit(1)

            if not _check_port_available(frontend_port):
                errors.append(
                    f"Port {frontend_port} (frontend) is already in use.\n"
                    f"  To free it: lsof -ti :{frontend_port} | xargs kill -9"
                )

        if errors:
            print("ERROR: Port(s) already in use:\n")
            for error in errors:
                print(f"  {error}\n")
            sys.exit(1)

    def _monitor_process(
        self,
        process: subprocess.Popen[str],
        name: str,
        log_file: IO[str] | None,
        patterns: list[str],
    ) -> None:
        is_ready = False
        # ``stdout`` is ``IO[Any] | None`` on ``Popen``; the caller
        # always launches the subprocess with ``stdout=PIPE``, but the
        # static contract still allows ``None``.  Bail out cleanly
        # rather than dereferencing ``None`` if the invariant breaks.
        if process.stdout is None:
            return
        stdout = process.stdout
        try:
            for line in iter(stdout.readline, ""):
                if not line:
                    break

                line = line.rstrip()
                if log_file is not None:
                    log_file.write(line + "\n")
                print(f"[{name}] {line}")

                if not is_ready and any(re.search(p, line, re.IGNORECASE) for p in patterns):
                    is_ready = True
                    if name == "backend":
                        self.backend_ready = True
                    else:
                        self.frontend_ready = True
                    print(f"  {name.capitalize()} is ready!")

                    if self.no_ui and self.backend_ready:
                        print(f"\n{'=' * 50}")
                        print("  Backend is ready! (running without UI)")
                        print(f"  API available at http://localhost:{self.port}")
                        print(f"{'=' * 50}\n")
                    elif self.backend_ready and self.frontend_ready:
                        print(f"\n{'=' * 50}")
                        print("  Both frontend and backend are ready!")
                        print(f"  Open the app at http://localhost:{self.port}")
                        print(f"{'=' * 50}\n")

            process.wait()
            if process.returncode != 0:
                self.failed.set()

        except Exception as exc:
            print(f"Error monitoring {name}: {exc}")
            self.failed.set()

    def _spawn_extractor(self) -> subprocess.Popen[str] | None:
        cmd = [sys.executable, "-m", "payloads.agent.runtime.extractor_service"]
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            print(f"WARNING: failed to start extractor service (file upload disabled): {exc}")
            return None

    def _pipe_extractor_output(self, process: subprocess.Popen[str]) -> None:
        """Forward the extractor's output to its log + stdout until it exits."""
        if process.stdout is None:
            process.wait()
            return
        for line in iter(process.stdout.readline, ""):
            if not line:
                break
            line = line.rstrip()
            if self.extractor_log is not None:
                self.extractor_log.write(line + "\n")
            print(f"[extractor] {line}")
        process.wait()

    def _supervise_extractor(self) -> None:
        """Keep the isolated extractor alive with bounded restarts.

        The extractor is isolated (D6): its exit must degrade file upload,
        never set ``self.failed`` (which would take down the agent).  A
        crashed worker is respawned with a fixed backoff up to a cap so a
        crash-loop cannot busy-spin; respawning stops once ``cleanup`` sets
        ``shutting_down``.
        """
        restarts = 0
        while not self.shutting_down:
            process = self._spawn_extractor()
            if process is None:
                return
            self.extractor_process = process
            print(f"Starting extractor (PID: {process.pid}) on port {_env.read(_env.FILE_UPLOAD_PORT)}")
            self._pipe_extractor_output(process)
            if self.shutting_down:
                return
            restarts += 1
            if restarts > _EXTRACTOR_MAX_RESTARTS:
                print("WARNING: extractor exceeded max restarts; file upload degraded until next app restart")
                return
            print(
                f"WARNING: extractor exited (code {process.returncode}); restarting "
                f"({restarts}/{_EXTRACTOR_MAX_RESTARTS}) in {_EXTRACTOR_RESTART_BACKOFF_SECONDS}s"
            )
            time.sleep(_EXTRACTOR_RESTART_BACKOFF_SECONDS)

    def _start_extractor(self) -> None:
        """Start the supervised extractor when ENABLE_FILE_UPLOAD is set.

        Best-effort: a failure to spawn (e.g. the ``extraction`` extra is
        not installed) logs a warning and leaves the agent + UI running.
        """
        if not _env.read_bool(_env.ENABLE_FILE_UPLOAD):
            return
        self.extractor_log = open("extractor.log", "w", buffering=1)
        thread = threading.Thread(target=self._supervise_extractor, daemon=True)
        thread.start()

    def _clone_frontend_if_needed(self) -> bool:
        chat_ui_dir = PROJECT_ROOT / CHAT_UI_DIR_NAME
        if chat_ui_dir.exists():
            return True

        print(f"Cloning {CHAT_UI_DIR_NAME}...")
        for url in [
            "https://github.com/databricks/app-templates.git",
            "git@github.com:databricks/app-templates.git",
        ]:
            try:
                subprocess.run(
                    ["git", "clone", "--filter=blob:none", "--sparse", url, "temp-app-templates"],
                    check=True,
                    capture_output=True,
                    cwd=str(PROJECT_ROOT),
                )
                break
            except subprocess.CalledProcessError:
                continue
        else:
            print("ERROR: Failed to clone repository.")
            print(
                "Manually download from: "
                "https://download-directory.github.io/?url="
                "https://github.com/databricks/app-templates/tree/main/e2e-chatbot-app-next"
            )
            return False

        temp_dir = PROJECT_ROOT / "temp-app-templates"
        subprocess.run(
            ["git", "sparse-checkout", "set", CHAT_UI_DIR_NAME],
            cwd=str(temp_dir),
            check=True,
        )
        try:
            subprocess.run(
                ["git", "checkout", UPSTREAM_TEMPLATE_SHA],
                cwd=str(temp_dir),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(
                f"ERROR: Failed to checkout pinned upstream commit {UPSTREAM_TEMPLATE_SHA}.\n"
                f"  Update UPSTREAM_TEMPLATE_SHA in {Path(__file__).name} and the matching\n"
                f"  entry in payloads/agent/ui/overrides/MANIFEST.md if upstream has moved.\n"
                f"  git stderr: {(exc.stderr or b'').decode(errors='replace').strip()}"
            )
            return False
        (temp_dir / CHAT_UI_DIR_NAME).rename(chat_ui_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return True

    @staticmethod
    def _apply_overrides() -> None:
        """Apply local overrides to the cloned chat-UI template.

        Overlays files from ``payloads/agent/ui/overrides/`` onto the cloned
        ``e2e-chatbot-app-next`` directory, then each package's
        ``ui/overrides/`` named in ``CHAT_UI_OVERLAYS``, in order, so a
        later overlay wins.  Each override replaces its upstream counterpart
        when the content differs (or the target doesn't exist).  See
        ``overrides/MANIFEST.md`` for what each file changes.
        """
        chat_ui_dir = PROJECT_ROOT / CHAT_UI_DIR_NAME
        for overlay_dir in [OVERRIDES_DIR, *_extra_overlay_dirs()]:
            ProcessManager._apply_overlay(overlay_dir, chat_ui_dir)

    @staticmethod
    def _apply_overlay(overlay_dir: Path, chat_ui_dir: Path) -> None:
        if not overlay_dir.exists():
            print(f"  No overrides dir at {overlay_dir}")
            return

        applied = 0
        for override_file in overlay_dir.rglob("*"):
            if override_file.is_dir() or override_file.name == "MANIFEST.md":
                continue
            rel = override_file.relative_to(overlay_dir)
            target = chat_ui_dir / rel
            if target.exists() and target.read_bytes() == override_file.read_bytes():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(override_file, target)
            applied += 1
            print(f"  Override: {rel}")
        if applied:
            print(f"  Applied {applied} override(s) from {overlay_dir} to {CHAT_UI_DIR_NAME}")

    def _start_process(
        self,
        cmd: list[str],
        name: str,
        log_file: IO[str] | None,
        patterns: list[str],
        cwd: str | None = None,
    ) -> subprocess.Popen[str]:
        print(f"Starting {name}...")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
        thread = threading.Thread(
            target=self._monitor_process,
            args=(process, name, log_file, patterns),
            daemon=True,
        )
        thread.start()
        return process

    def _build_chat_ui(self, chat_ui_dir: Path) -> bool:
        """Install deps and build the chat UI, step by step.

        The Lakebase ``db:migrate`` step is intentionally **non-fatal**:
        after an App service-principal rotation the new SP may not yet own
        the Lakebase schemas the previous SP created, so the migration can
        fail with "permission denied for schema" until the SP's Postgres
        role + grants propagate.  That condition is transient and
        self-healing (it succeeds on the next restart), and it must never
        crash the whole app -- doing so also takes down the agent backend.
        The actual ``build:client`` / ``build:server`` bundles stay fatal.

        Returns ``False`` when a fatal step fails (caller aborts startup).
        """
        for cmd_str, fatal in _CHAT_UI_BUILD_STEPS:
            if not self._run_npm_step(cmd_str, chat_ui_dir, fatal=fatal):
                return False
        return True

    def _run_npm_step(self, cmd_str: str, chat_ui_dir: Path, *, fatal: bool) -> bool:
        """Run one chat-UI build command.

        A failing *fatal* step returns ``False`` (startup aborts).  A
        failing *non-fatal* step logs a warning and returns ``True`` so
        startup continues.
        """
        print(f"Running {cmd_str} ...")
        result = subprocess.run(
            cmd_str.split(),
            cwd=str(chat_ui_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True

        combined = (result.stdout or "") + (result.stderr or "")
        if fatal:
            print(f"{cmd_str} failed: {combined}")
            return False

        print(
            f"WARNING: {cmd_str} failed (non-fatal) -- continuing startup.\n"
            "  Chat-history persistence may be degraded until the App service\n"
            "  principal's Lakebase grants propagate (heals on a later restart).\n"
            f"{combined}"
        )
        return True

    def _print_logs(self, log_path: str) -> None:
        print(f"\nLast 50 lines of {log_path}:")
        print("-" * 40)
        try:
            lines = Path(log_path).read_text().splitlines()
            print("\n".join(lines[-50:]))
        except FileNotFoundError:
            print(f"(no {log_path} found)")
        print("-" * 40)

    def cleanup(self) -> None:
        # Signal the extractor supervisor first so it does not respawn the
        # child we are about to terminate.
        self.shutting_down = True
        label = "Shutting down..." if self.no_ui else "Shutting down both processes..."
        print(f"\n{'=' * 42}")
        print(label)
        print(f"{'=' * 42}")

        for proc in [self.backend_process, self.frontend_process, self.extractor_process]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, Exception):
                    proc.kill()

        if self.backend_log:
            self.backend_log.close()
        if self.frontend_log:
            self.frontend_log.close()
        if self.extractor_log:
            self.extractor_log.close()

    def run(self, backend_args: list[str] | None = None) -> int:
        if not _env.read_optional(_env.DATABRICKS_APP_NAME):
            self.check_ports()

        if not self.no_ui:
            if not self._clone_frontend_if_needed():
                print("WARNING: Failed to clone frontend. Continuing with backend only.")
                self.no_ui = True
            else:
                self._apply_overrides()
                os.environ[_env.API_PROXY.name] = f"http://localhost:{self.port}/invocations"

        self.backend_log = open("backend.log", "w", buffering=1)
        if not self.no_ui:
            self.frontend_log = open("frontend.log", "w", buffering=1)

        try:
            # Spawn ``start_server`` via ``python -m`` so it works
            # whether the framework is on disk (source checkout) or
            # only in the App venv (wheel install) -- issue #112.
            backend_cmd = [
                sys.executable,
                "-m",
                "payloads.agent.runtime.start_server",
                "--port",
                str(self.port),
            ]
            if backend_args:
                backend_cmd.extend(backend_args)

            self.backend_process = self._start_process(
                backend_cmd,
                "backend",
                self.backend_log,
                BACKEND_READY_PATTERNS,
            )

            self._start_extractor()

            if not self.no_ui:
                chat_ui_dir = PROJECT_ROOT / CHAT_UI_DIR_NAME

                if not self._build_chat_ui(chat_ui_dir):
                    return 1

                self.frontend_process = self._start_process(
                    ["npm", "run", "start"],
                    "frontend",
                    self.frontend_log,
                    FRONTEND_READY_PATTERNS,
                    cwd=str(chat_ui_dir),
                )

                print(
                    f"\nMonitoring processes "
                    f"(Backend PID: {self.backend_process.pid}, "
                    f"Frontend PID: {self.frontend_process.pid})\n"
                )
            else:
                print(f"\nMonitoring backend process (PID: {self.backend_process.pid})\n")

            while not self.failed.is_set():
                time.sleep(0.1)
                if self.backend_process.poll() is not None:
                    self.failed.set()
                    break
                if not self.no_ui and self.frontend_process and self.frontend_process.poll() is not None:
                    self.failed.set()
                    break

            # Annotate ``failed_proc`` explicitly: ``backend_process``
            # and ``frontend_process`` are both ``Popen | None`` on the
            # instance, but mypy narrows the first assignment from the
            # truthy ``self.backend_process and ...`` checks in the loop
            # above and then complains about the ``frontend_process``
            # branch.  Annotating the union once keeps both arms valid.
            failed_proc: subprocess.Popen[str] | None
            if self.no_ui or self.backend_process.poll() is not None:
                failed_name = "backend"
                failed_proc = self.backend_process
            else:
                failed_name = "frontend"
                failed_proc = self.frontend_process
            exit_code = failed_proc.returncode if failed_proc else 1

            print(f"\n{'=' * 42}\nERROR: {failed_name} process exited with code {exit_code}\n{'=' * 42}")
            self._print_logs("backend.log")
            if not self.no_ui:
                self._print_logs("frontend.log")
            return exit_code

        except KeyboardInterrupt:
            print("\nInterrupted")
            return 0

        finally:
            self.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start bundled agent backend + chat UI",
        usage="%(prog)s [OPTIONS]\n\nAll extra options are passed through to start_server.py.",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run backend only, skip frontend UI",
    )
    args, backend_args = parser.parse_known_args(argv)

    port = 8000
    for i, arg in enumerate(backend_args):
        if arg == "--port" and i + 1 < len(backend_args):
            try:
                port = int(backend_args[i + 1])
            except ValueError:
                pass
            break

    return ProcessManager(port=port, no_ui=args.no_ui).run(backend_args)


if __name__ == "__main__":
    raise SystemExit(main())
