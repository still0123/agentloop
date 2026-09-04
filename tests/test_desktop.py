from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from agentloop import desktop


class _Event:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


def test_desktop_stops_server_and_marks_window_close(
    tmp_path: Path, monkeypatch
) -> None:
    stopped = threading.Event()
    events = []
    marker = tmp_path / "quit"

    class FakeServer:
        server_port = 45837

        def serve_forever(self) -> None:
            events.append("serve")
            stopped.wait(2)

        def shutdown(self) -> None:
            events.append("shutdown")
            stopped.set()

        def server_close(self) -> None:
            events.append("close")

    class FakeWindow:
        def __init__(self) -> None:
            self.events = type("Events", (), {"closed": _Event()})()

    class FakeWebView:
        def __init__(self) -> None:
            self.window = FakeWindow()

        def create_window(self, title, url, **kwargs):
            assert title == f"AgentLoop - {tmp_path.name}"
            assert url == "http://127.0.0.1:45837"
            assert kwargs["min_size"] == (900, 600)
            return self.window

        def start(self) -> None:
            for callback in self.window.events.closed.callbacks:
                callback()

    monkeypatch.setenv("AGENTLOOP_QUIT_MARKER", str(marker))
    monkeypatch.setattr(desktop, "_load_webview", FakeWebView)
    monkeypatch.setattr(
        desktop, "build_default_agent", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(desktop, "make_server", lambda *args, **kwargs: FakeServer())

    desktop.run_desktop(tmp_path)

    assert marker.exists()
    assert events[-2:] == ["shutdown", "close"]


def test_macos_launcher_forces_exit_after_close_timeout(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    app_bin = tmp_path / "AgentLoop.app" / "Contents" / "MacOS" / "AgentLoop.bin"
    launcher = app_bin.with_name("AgentLoop")
    app_bin.parent.mkdir(parents=True)
    shutil.copy2(root / "scripts" / "macos_app_launcher.sh", launcher)
    launcher.chmod(0o755)
    app_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['AGENTLOOP_QUIT_MARKER']).touch()\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    app_bin.chmod(0o755)
    env = os.environ.copy()
    env["AGENTLOOP_QUIT_TIMEOUT_SECONDS"] = "0"
    env["AGENTLOOP_KILL_GRACE_SECONDS"] = "0"
    env["TMPDIR"] = str(tmp_path)

    started = time.monotonic()
    result = subprocess.run([launcher], env=env, timeout=10, check=False)

    assert result.returncode == 0
    assert time.monotonic() - started < 8
