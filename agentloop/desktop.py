"""Native desktop shell for the local AgentLoop web interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from .cli import _load_dotenv, build_default_agent
from .web import SessionStore, WebState, make_server

SHUTDOWN_TIMEOUT = 5


class DesktopError(RuntimeError):
    """Raised when the native desktop shell cannot start."""


class _WindowEvents(Protocol):
    closed: object


class _Window(Protocol):
    events: _WindowEvents


class _WebView(Protocol):
    def create_window(
        self,
        title: str,
        url: str,
        *,
        width: int,
        height: int,
        min_size: tuple[int, int],
    ) -> _Window: ...

    def start(self) -> None: ...


def resolve_workdir(workdir: Path | None = None) -> Path:
    """Resolve the desktop workspace after loading the per-user config."""
    config = Path(
        os.environ.get("AGENTLOOP_ENV_FILE", "~/.agentloop/.env")
    ).expanduser()
    _load_dotenv(str(config))
    configured = os.environ.get("AGENTLOOP_WORKDIR")
    selected = workdir or Path(configured or Path.home() / ".agentloop" / "workspace")
    selected = selected.expanduser().resolve()
    if workdir is None and not configured:
        selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not selected.is_dir():
        raise DesktopError(f"Workspace does not exist: {selected}")
    _load_dotenv(str(selected / ".env"))
    return selected


def run_desktop(workdir: Path | None = None) -> None:
    """Serve AgentLoop on loopback and display it in a native window."""
    selected = resolve_workdir(workdir)
    webview = _load_webview()
    state = WebState(store=SessionStore(selected))
    try:
        state.agent = build_default_agent(
            selected,
            verbose_tools=False,
            ask_user=state.ask_user,
            should_stop=state.cancelled,
        )
    except SystemExit as exc:
        raise DesktopError(str(exc) or "Model configuration is missing.") from exc

    server = make_server(state, os.urandom(24).hex())
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="agentloop-desktop-server",
        daemon=True,
    )
    server_thread.start()
    closing = threading.Event()

    def shutdown() -> None:
        if closing.is_set():
            return
        closing.set()
        state.stop()
        state.wait(SHUTDOWN_TIMEOUT)
        server.shutdown()

    def request_shutdown(*_args: object) -> None:
        _mark_quit_requested()
        shutdown()

    try:
        window = webview.create_window(
            f"AgentLoop - {selected.name}",
            f"http://127.0.0.1:{server.server_port}",
            width=1280,
            height=820,
            min_size=(900, 600),
        )
        window.events.closed += request_shutdown  # type: ignore[operator]
        webview.start()
    finally:
        shutdown()
        server_thread.join(timeout=SHUTDOWN_TIMEOUT)
        server.server_close()


def _mark_quit_requested() -> None:
    marker = os.environ.get("AGENTLOOP_QUIT_MARKER")
    if not marker:
        return
    with suppress(OSError):
        Path(marker).touch()


def _load_webview() -> _WebView:
    try:
        module: ModuleType = import_module("webview")
    except ModuleNotFoundError as exc:
        raise DesktopError(
            "Desktop support is not installed. Run: pip install -e '.[desktop]'"
        ) from exc
    return cast(_WebView, module)


def _show_startup_error(message: str) -> None:
    if sys.platform != "darwin":
        print(f"AgentLoop: {message}", file=sys.stderr)
        return
    script = f'display alert "AgentLoop" message {json.dumps(message)} as critical'
    with suppress(OSError):
        subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


def main() -> int:
    try:
        run_desktop()
    except Exception as exc:  # noqa: BLE001
        _show_startup_error(str(exc) or "AgentLoop could not start.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
