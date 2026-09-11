"""Bounded command capture with recoverable output, independent of task domain.

This is process lifecycle management, not a shell sandbox. Authorization belongs
to the caller's permission policy. Outputs may contain sensitive data and stay in
private local artifacts; only a short preview is returned to the model.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

MAX_CAPTURE_BYTES = 16 * 1024 * 1024  # per stream; exceeding it stops the process
PREVIEW_BYTES = 6_000


def _terminate(proc):
    # The shell may have exited while descendants still own the output pipes.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
    proc.wait()


def run_command(workdir: Path, command: str, timeout: int, should_stop) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 600
    ):
        raise ValueError("timeout must be an integer between 1 and 600 seconds")
    if should_stop():
        return "Error: command cancelled by user"

    from .tools import safe_path

    directory = safe_path(workdir, ".task_outputs/commands")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = uuid.uuid4().hex
    paths = {name: directory / f"{ident}.{name}.txt" for name in ("stdout", "stderr")}
    sizes = dict.fromkeys(paths, 0)
    status = ""
    with ExitStack() as stack:
        files = {
            name: stack.enter_context(
                os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                )
            )
            for name, path in paths.items()
        }
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stack.callback(proc.stderr.close)
        stack.callback(proc.stdout.close)
        selector = stack.enter_context(selectors.DefaultSelector())
        for name in paths:
            selector.register(getattr(proc, name), selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map() or proc.poll() is None:
                if should_stop():
                    status = "Error: command cancelled by user"
                    break
                if time.monotonic() >= deadline:
                    status = f"Error: command timed out after {timeout}s"
                    break
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    room = MAX_CAPTURE_BYTES - sizes[name]
                    files[name].write(chunk[:room])
                    sizes[name] += min(room, len(chunk))
                    if len(chunk) > room:
                        status = (
                            "Error: command output limit exceeded; "
                            "artifacts are partial"
                        )
                        break
                if status:
                    break
            if status:
                _terminate(proc)
            else:
                proc.wait()
        except BaseException:
            _terminate(proc)
            raise

    parts = ([status] if status else []) + [f"exit={proc.returncode}"]
    for name, path in paths.items():
        parts.append(
            f"[{name}] artifact={path.relative_to(workdir)} bytes={sizes[name]}"
        )
        with path.open("rb") as handle:
            preview = handle.read(PREVIEW_BYTES)
            if sizes[name] > PREVIEW_BYTES:
                handle.seek(max(PREVIEW_BYTES, sizes[name] - PREVIEW_BYTES // 2))
                preview += (
                    b"\n... (preview shortened; read/search artifact) ...\n"
                    + handle.read()
                )
        if preview:
            parts.append(preview.decode("utf-8", errors="replace"))
    if status:
        parts.append(
            "Output capture is incomplete; absence in this artifact "
            "is not proof of absence."
        )
    return "\n".join(parts)
