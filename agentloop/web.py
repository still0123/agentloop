"""Local Web UI bridge for AgentLoop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cli import _load_dotenv, build_default_agent

MAX_BODY_BYTES = 1_000_000
PERMISSION_TIMEOUT = 300
SESSION_VERSION = 1
PERSISTED_EVENT_TYPES = {
    "run_start",
    "assistant_message",
    "tool_call",
    "tool_result",
    "done",
    "error",
}


class SessionStore:
    """Persist one Web session per workspace as an atomic local JSON file."""

    def __init__(self, workdir: Path, root: Path | None = None) -> None:
        self.workspace = str(workdir.resolve())
        if root is None:
            configured = os.environ.get("AGENTLOOP_HOME")
            root = (
                Path(configured).expanduser()
                if configured
                else Path.home() / ".agentloop"
            )
        digest = hashlib.sha256(self.workspace.encode()).hexdigest()[:20]
        self.path = Path(root) / "sessions" / f"{digest}.json"

    def load(self) -> tuple[list, list]:
        if not self.path.is_file():
            return [], []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != SESSION_VERSION:
                raise ValueError("unsupported session version")
            if data.get("workspace") != self.workspace:
                raise ValueError("session workspace mismatch")
            messages = data.get("messages")
            events = data.get("events")
            if not isinstance(messages, list) or not isinstance(events, list):
                raise ValueError("invalid session payload")
            return messages, events
        except (OSError, ValueError) as exc:
            print(
                f"Warning: could not load session {self.path}: {exc}", file=sys.stderr
            )
            return [], []

    def save(self, messages: list, events: list) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        payload = {
            "version": SESSION_VERSION,
            "workspace": self.workspace,
            "messages": messages,
            "events": events,
        }
        try:
            with temp.open("x", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class WebState:
    """Connect one Agent session to browser event and permission queues."""

    def __init__(
        self,
        permission_timeout: float = PERMISSION_TIMEOUT,
        store: SessionStore | None = None,
    ) -> None:
        self.agent = None
        self.permission_timeout = permission_timeout
        self.store = store
        self.messages, self.events = store.load() if store else ([], [])
        self._active = False
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._permissions: dict[str, queue.Queue] = {}

    def subscribe(self) -> queue.Queue:
        events: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.add(events)
        return events

    def unsubscribe(self, events: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(events)

    def emit(self, event: dict) -> None:
        with self._lock:
            if event.get("type") in PERSISTED_EVENT_TYPES:
                self.events.append(dict(event))
            subscribers = tuple(self._subscribers)
        for events in subscribers:
            events.put(event)

    def start(self, prompt: str) -> bool:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        with self._lock:
            if self._active:
                return False
            self._active = True
        thread = threading.Thread(target=self._run, args=(prompt,), daemon=True)
        thread.start()
        return True

    def _run(self, prompt: str) -> None:
        try:
            if self.agent is None:
                self.emit(
                    {
                        "type": "error",
                        "error": "RuntimeError",
                        "message": "web agent is not configured",
                    }
                )
                return
            result = self.agent.run(prompt, self.messages, on_event=self.emit)
            with self._lock:
                self.messages = result.messages
        except Exception:
            pass  # Agent.run emits the error event before re-raising.
        finally:
            if self.store:
                try:
                    self.store.save(self.messages, self.events)
                except Exception as exc:  # noqa: BLE001
                    self.emit(
                        {
                            "type": "session_error",
                            "message": f"Could not save session: {exc}",
                        }
                    )
            with self._lock:
                self._active = False

    def snapshot(self) -> dict:
        with self._lock:
            return {"events": list(self.events), "active": self._active}

    def reset(self) -> bool:
        with self._lock:
            if self._active:
                return False
            if self.store:
                self.store.clear()
            self.messages = []
            self.events = []
        self.emit({"type": "session_reset"})
        return True

    def ask_user(self, tool_name: str, args: dict, reason: str) -> bool:
        request_id = secrets.token_urlsafe(12)
        answer: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._permissions[request_id] = answer
        self.emit(
            {
                "type": "permission_request",
                "id": request_id,
                "tool": tool_name,
                "input": args,
                "reason": reason,
            }
        )
        timed_out = False
        try:
            allowed = bool(answer.get(timeout=self.permission_timeout))
        except queue.Empty:
            allowed = False
            timed_out = True
        finally:
            with self._lock:
                self._permissions.pop(request_id, None)
        self.emit(
            {
                "type": "permission_resolved",
                "id": request_id,
                "allowed": allowed,
                "timed_out": timed_out,
            }
        )
        return allowed

    def resolve_permission(self, request_id: str, allowed: bool) -> bool:
        with self._lock:
            answer = self._permissions.get(request_id)
        if answer is None:
            return False
        try:
            answer.put_nowait(allowed)
        except queue.Full:
            return False
        return True


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(state: WebState, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._local_host():
                self._json(HTTPStatus.FORBIDDEN, {"error": "loopback host required"})
                return
            target = urlsplit(self.path)
            if target.path == "/":
                page = (
                    Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8")
                )
                self._send(
                    HTTPStatus.OK,
                    page.replace("__AGENTLOOP_TOKEN__", json.dumps(token)).encode(),
                    "text/html; charset=utf-8",
                )
                return
            if target.path == "/api/events":
                supplied = parse_qs(target.query).get("token", [""])[0]
                if not secrets.compare_digest(supplied, token):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
                    return
                self._events()
                return
            if target.path == "/api/session":
                if not self._authorized():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
                    return
                self._json(HTTPStatus.OK, state.snapshot())
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
                return
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if self.path == "/api/run":
                prompt = payload.get("prompt")
                if not isinstance(prompt, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "prompt must be a string"}
                    )
                    return
                try:
                    started = state.start(prompt)
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                if not started:
                    self._json(HTTPStatus.CONFLICT, {"error": "an agent run is active"})
                    return
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
                return

            if self.path == "/api/session/reset":
                if not state.reset():
                    self._json(HTTPStatus.CONFLICT, {"error": "an agent run is active"})
                    return
                self._json(HTTPStatus.OK, {"ok": True})
                return

            prefix = "/api/permissions/"
            if self.path.startswith(prefix):
                request_id = self.path[len(prefix) :]
                allowed = payload.get("allowed")
                if not request_id or not isinstance(allowed, bool):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "permission id and boolean allowed are required"},
                    )
                    return
                if not state.resolve_permission(request_id, allowed):
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "permission request not found"},
                    )
                    return
                self._json(HTTPStatus.OK, {"ok": True})
                return

            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _events(self) -> None:
            events = state.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    try:
                        event = events.get(timeout=15)
                        data = json.dumps(event, ensure_ascii=False, default=str)
                        chunk = f"data: {data}\n\n".encode()
                    except queue.Empty:
                        chunk = b": keepalive\n\n"
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.unsubscribe(events)

        def _authorized(self) -> bool:
            if not self._local_host():
                return False
            supplied = self.headers.get("X-AgentLoop-Token", "")
            return secrets.compare_digest(supplied, token)

        def _local_host(self) -> bool:
            host = self.headers.get("Host", "")
            try:
                hostname = urlsplit(f"//{host}").hostname
            except ValueError:
                return False
            return hostname in {"127.0.0.1", "localhost"}

        def _read_json(self) -> dict:
            content_type = self.headers.get("Content-Type", "").partition(";")[0]
            if content_type.strip().lower() != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request body size")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _json(self, status: HTTPStatus, payload: dict) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def make_server(state: WebState, token: str, port: int = 0) -> LocalHTTPServer:
    return LocalHTTPServer(("127.0.0.1", port), make_handler(state, token))


def serve(workdir: Path, port: int = 0, open_browser: bool = True) -> int:
    state = WebState(store=SessionStore(workdir))
    state.agent = build_default_agent(
        workdir, verbose_tools=False, ask_user=state.ask_user
    )
    token = secrets.token_urlsafe(24)
    server = make_server(state, token, port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"AgentLoop web: {url}", flush=True)
    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop web")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    _load_dotenv()
    return serve(Path.cwd(), port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
