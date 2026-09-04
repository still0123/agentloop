from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agentloop import desktop


class _Event:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


def _make_launcher(tmp_path: Path, child_source: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    app_bin = tmp_path / "AgentLoop.app" / "Contents" / "MacOS" / "AgentLoop.bin"
    launcher = app_bin.with_name("AgentLoop")
    app_bin.parent.mkdir(parents=True)
    shutil.copy2(root / "scripts" / "macos_app_launcher.sh", launcher)
    launcher.chmod(0o755)
    app_bin.write_text(child_source, encoding="utf-8")
    app_bin.chmod(0o755)
    return launcher


def test_resolve_workdir_uses_isolated_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTLOOP_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("AGENTLOOP_WORKDIR", raising=False)

    selected = desktop.resolve_workdir()

    assert selected == tmp_path / ".agentloop" / "workspace"
    assert selected.is_dir()


def test_desktop_stops_server_and_marks_window_close(
    tmp_path: Path, monkeypatch
) -> None:
    stopped = threading.Event()
    events = []
    waited = []
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
    monkeypatch.setattr(
        desktop.WebState,
        "wait",
        lambda _self, timeout: waited.append(timeout) or True,
        raising=False,
    )

    desktop.run_desktop(tmp_path)

    assert marker.exists()
    assert waited == [5]
    assert events[-2:] == ["shutdown", "close"]


def test_desktop_runtime_error_is_not_marked_as_clean_close(
    tmp_path: Path, monkeypatch
) -> None:
    stopped = threading.Event()
    marker = tmp_path / "quit"

    class FakeServer:
        server_port = 45837

        def serve_forever(self) -> None:
            stopped.wait(2)

        def shutdown(self) -> None:
            stopped.set()

        def server_close(self) -> None:
            pass

    class FakeWindow:
        def __init__(self) -> None:
            self.events = type("Events", (), {"closed": _Event()})()

    class BrokenWebView:
        def create_window(self, *_args, **_kwargs):
            return FakeWindow()

        def start(self) -> None:
            raise RuntimeError("window failed")

    monkeypatch.setenv("AGENTLOOP_QUIT_MARKER", str(marker))
    monkeypatch.setattr(desktop, "_load_webview", BrokenWebView)
    monkeypatch.setattr(
        desktop, "build_default_agent", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(desktop, "make_server", lambda *args, **kwargs: FakeServer())
    monkeypatch.setattr(desktop.WebState, "wait", lambda *_args: True, raising=False)

    with pytest.raises(RuntimeError, match="window failed"):
        desktop.run_desktop(tmp_path)

    assert not marker.exists()


def test_desktop_main_returns_failure_for_startup_error(monkeypatch) -> None:
    errors = []

    def fail() -> None:
        raise desktop.DesktopError("missing model")

    monkeypatch.setattr(desktop, "run_desktop", fail)
    monkeypatch.setattr(desktop, "_show_startup_error", errors.append)

    assert desktop.main() == 1
    assert errors == ["missing model"]


def test_macos_launcher_forces_exit_after_close_timeout(tmp_path: Path) -> None:
    launcher = _make_launcher(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['AGENTLOOP_QUIT_MARKER']).touch()\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
    )
    env = os.environ.copy()
    env["AGENTLOOP_QUIT_TIMEOUT_SECONDS"] = "0"
    env["AGENTLOOP_KILL_GRACE_SECONDS"] = "0"
    env["TMPDIR"] = str(tmp_path)

    started = time.monotonic()
    result = subprocess.run([launcher], env=env, timeout=10, check=False)

    assert result.returncode == 0
    assert time.monotonic() - started < 8


def test_macos_launcher_forces_exit_after_external_signal(tmp_path: Path) -> None:
    launcher = _make_launcher(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
    )
    env = os.environ.copy()
    env["AGENTLOOP_QUIT_TIMEOUT_SECONDS"] = "0"
    env["AGENTLOOP_KILL_GRACE_SECONDS"] = "0"
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.Popen([launcher], env=env, start_new_session=True)

    try:
        time.sleep(0.5)
        proc.terminate()
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)


def test_package_always_rebuilds_when_skip_build_is_set(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    scripts = tmp_path / "scripts"
    tools = tmp_path / "bin"
    scripts.mkdir()
    tools.mkdir()
    package = scripts / "package_macos_app.sh"
    shutil.copy2(root / "scripts" / "package_macos_app.sh", package)
    package.chmod(0o755)
    build = scripts / "build_macos_app.sh"
    build.write_text(
        '#!/usr/bin/env bash\nprintf built > "$(dirname "$0")/../build-called"\n'
        "exit 23\n",
        encoding="utf-8",
    )
    build.chmod(0o755)
    uname = tools / "uname"
    uname.write_text("#!/usr/bin/env bash\nprintf Darwin\n", encoding="utf-8")
    uname.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tools}:{env['PATH']}"
    env["SKIP_BUILD"] = "1"

    result = subprocess.run([package], env=env, check=False)

    assert result.returncode == 23
    assert (tmp_path / "build-called").read_text() == "built"
