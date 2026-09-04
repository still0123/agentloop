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
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cli import _load_dotenv, build_default_agent

MAX_BODY_BYTES = 1_000_000
PERMISSION_TIMEOUT = 300
SESSION_VERSION = 2
MAX_SESSIONS = 20
MAX_SESSION_EVENTS = 500
MAX_EVENT_TEXT = 20_000
PERSISTED_EVENT_TYPES = {
    "run_start",
    "assistant_message",
    "tool_call",
    "tool_result",
    "done",
    "error",
}


class SessionStore:
    """Persist bounded Web sessions per workspace in one atomic JSON catalog."""

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
        self._lock = threading.RLock()
        self._data = self._load_catalog()

    @property
    def active_id(self) -> str:
        with self._lock:
            return self._data["active_id"]

    def load(self) -> tuple[list, list]:
        with self._lock:
            session = self._active()
            return list(session["messages"]), list(session["events"])

    def save(self, messages: list, events: list) -> None:
        with self._lock:
            session = self._active()
            session["messages"] = messages
            session["events"] = [
                _limit_persisted_value(event) for event in events[-MAX_SESSION_EVENTS:]
            ]
            session["updated_at"] = time.time()
            if session["title"] == "新会话":
                session["title"] = _title_from_events(events)
            self._write()

    def clear(self) -> None:
        with self._lock:
            session = self._active()
            session["messages"] = []
            session["events"] = []
            session["updated_at"] = time.time()
            self._write()

    def list_sessions(self) -> list[dict]:
        with self._lock:
            sessions = sorted(
                self._data["sessions"].values(),
                key=lambda session: session["updated_at"],
                reverse=True,
            )
            return [
                {
                    "id": session["id"],
                    "title": session["title"],
                    "created_at": session["created_at"],
                    "updated_at": session["updated_at"],
                }
                for session in sessions
            ]

    def create(self, title: str = "新会话") -> str:
        with self._lock:
            if len(self._data["sessions"]) >= MAX_SESSIONS:
                raise ValueError(f"session limit reached ({MAX_SESSIONS})")
            session = self._new_session(title)
            self._data["sessions"][session["id"]] = session
            self._data["active_id"] = session["id"]
            self._write()
            return session["id"]

    def activate(self, session_id: str) -> None:
        with self._lock:
            self._require(session_id)
            self._data["active_id"] = session_id
            self._write()

    def rename(self, session_id: str, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("session title must not be empty")
        with self._lock:
            session = self._require(session_id)
            session["title"] = title[:80]
            session["updated_at"] = time.time()
            self._write()

    def delete(self, session_id: str) -> str:
        with self._lock:
            self._require(session_id)
            del self._data["sessions"][session_id]
            if not self._data["sessions"]:
                session = self._new_session()
                self._data["sessions"][session["id"]] = session
            if self._data["active_id"] == session_id:
                replacement = max(
                    self._data["sessions"].values(),
                    key=lambda session: session["updated_at"],
                )
                self._data["active_id"] = replacement["id"]
            self._write()
            return self._data["active_id"]

    def _load_catalog(self) -> dict:
        if not self.path.is_file():
            return self._empty_catalog()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("session catalog must be an object")
            if data.get("workspace") != self.workspace:
                raise ValueError("session workspace mismatch")
            if data.get("version") == 1:
                return self._migrate_v1(data)
            if data.get("version") != SESSION_VERSION:
                raise ValueError("unsupported session version")
            sessions = data.get("sessions")
            active_id = data.get("active_id")
            if not isinstance(sessions, dict) or active_id not in sessions:
                raise ValueError("invalid session catalog")
            for session in sessions.values():
                self._validate_session(session)
            return data
        except (OSError, ValueError) as exc:
            print(
                f"Warning: could not load session {self.path}: {exc}", file=sys.stderr
            )
            return self._empty_catalog()

    def _empty_catalog(self) -> dict:
        session = self._new_session()
        return {
            "version": SESSION_VERSION,
            "workspace": self.workspace,
            "active_id": session["id"],
            "sessions": {session["id"]: session},
        }

    def _migrate_v1(self, data: dict) -> dict:
        messages = data.get("messages")
        events = data.get("events")
        if not isinstance(messages, list) or not isinstance(events, list):
            raise ValueError("invalid legacy session payload")
        session = self._new_session(
            _title_from_events(events),
            messages=messages,
            events=events,
        )
        return {
            "version": SESSION_VERSION,
            "workspace": self.workspace,
            "active_id": session["id"],
            "sessions": {session["id"]: session},
        }

    def _new_session(
        self,
        title: str = "新会话",
        messages: list | None = None,
        events: list | None = None,
    ) -> dict:
        now = time.time()
        return {
            "id": secrets.token_hex(8),
            "title": title.strip()[:80] or "新会话",
            "created_at": now,
            "updated_at": now,
            "messages": list(messages or []),
            "events": [
                _limit_persisted_value(event)
                for event in (events or [])[-MAX_SESSION_EVENTS:]
            ],
        }

    def _active(self) -> dict:
        return self._data["sessions"][self._data["active_id"]]

    def _require(self, session_id: str) -> dict:
        session = self._data["sessions"].get(session_id)
        if session is None:
            raise KeyError("session not found")
        return session

    @staticmethod
    def _validate_session(session: dict) -> None:
        required = ("id", "title", "created_at", "updated_at", "messages", "events")
        if not isinstance(session, dict) or any(key not in session for key in required):
            raise ValueError("invalid session entry")
        if not isinstance(session["messages"], list) or not isinstance(
            session["events"], list
        ):
            raise ValueError("invalid session history")

    def _write(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)


def _title_from_events(events: list) -> str:
    for event in events:
        if event.get("type") == "run_start":
            prompt = str(event.get("prompt", "")).strip()
            if prompt:
                return prompt[:40]
    return "新会话"


def _limit_persisted_value(value):
    if isinstance(value, str):
        if len(value) <= MAX_EVENT_TEXT:
            return value
        return value[:MAX_EVENT_TEXT] + f"\n... (truncated, {len(value)} chars total)"
    if isinstance(value, list):
        return [_limit_persisted_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _limit_persisted_value(item) for key, item in value.items()}
    return value


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
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._permissions: dict[str, tuple[queue.Queue, dict]] = {}

    def subscribe(self) -> queue.Queue:
        events: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.add(events)
        return events

    def unsubscribe(self, events: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(events)

    def emit(self, event: dict) -> None:
        self._publish(event, persist=True)

    def _publish(self, event: dict, persist: bool) -> None:
        with self._lock:
            if persist and event.get("type") in PERSISTED_EVENT_TYPES:
                self.events.append(dict(event))
                del self.events[:-MAX_SESSION_EVENTS]
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
            self._cancelled.clear()
            self._active = True
        thread = threading.Thread(target=self._run, args=(prompt,), daemon=True)
        thread.start()
        return True

    def _run(self, prompt: str) -> None:
        terminal_event = None

        def emit(event: dict) -> None:
            nonlocal terminal_event
            if event.get("type") in {"done", "error"}:
                terminal_event = dict(event)
                return
            self.emit(event)

        try:
            if self.agent is None:
                terminal_event = {
                    "type": "error",
                    "error": "RuntimeError",
                    "message": "web agent is not configured",
                }
                return
            result = self.agent.run(prompt, self.messages, on_event=emit)
            with self._lock:
                self.messages = result.messages
        except Exception:
            pass  # Agent.run emits the error event before re-raising.
        finally:
            with self._lock:
                if terminal_event:
                    self.events.append(terminal_event)
                    del self.events[:-MAX_SESSION_EVENTS]
                messages = list(self.messages)
                events = list(self.events)
            if self.store:
                try:
                    self.store.save(messages, events)
                except Exception as exc:  # noqa: BLE001
                    self.emit(
                        {
                            "type": "session_error",
                            "message": f"Could not save session: {exc}",
                        }
                    )
            with self._lock:
                self._active = False
            if terminal_event:
                self._publish(terminal_event, persist=False)

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = {
                "events": list(self.events),
                "active": self._active,
                "cancelling": self._active and self._cancelled.is_set(),
                "permissions": [dict(event) for _, event in self._permissions.values()],
            }
            if self.store:
                snapshot["active_id"] = self.store.active_id
                snapshot["sessions"] = self.store.list_sessions()
            return snapshot

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def stop(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._cancelled.set()
            answers = tuple(answer for answer, _ in self._permissions.values())
        self.emit({"type": "cancel_requested"})
        for answer in answers:
            try:
                answer.put_nowait(False)
            except queue.Full:
                pass
        return True

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

    def create_session(self, title: str = "新会话") -> dict:
        with self._lock:
            self._require_idle()
            if not self.store:
                raise RuntimeError("session persistence is disabled")
            self.store.create(title)
            self.messages, self.events = self.store.load()
        self.emit({"type": "session_changed"})
        return self.snapshot()

    def activate_session(self, session_id: str) -> dict:
        with self._lock:
            self._require_idle()
            if not self.store:
                raise RuntimeError("session persistence is disabled")
            self.store.activate(session_id)
            self.messages, self.events = self.store.load()
        self.emit({"type": "session_changed"})
        return self.snapshot()

    def rename_session(self, session_id: str, title: str) -> dict:
        with self._lock:
            self._require_idle()
            if not self.store:
                raise RuntimeError("session persistence is disabled")
            self.store.rename(session_id, title)
        self.emit({"type": "session_list_changed"})
        return self.snapshot()

    def delete_session(self, session_id: str) -> dict:
        with self._lock:
            self._require_idle()
            if not self.store:
                raise RuntimeError("session persistence is disabled")
            was_active = session_id == self.store.active_id
            self.store.delete(session_id)
            if was_active:
                self.messages, self.events = self.store.load()
        self.emit({"type": "session_changed" if was_active else "session_list_changed"})
        return self.snapshot()

    def _require_idle(self) -> None:
        if self._active:
            raise RuntimeError("an agent run is active")

    def ask_user(self, tool_name: str, args: dict, reason: str) -> bool:
        request_id = secrets.token_urlsafe(12)
        answer: queue.Queue = queue.Queue(maxsize=1)
        event = {
            "type": "permission_request",
            "id": request_id,
            "tool": tool_name,
            "input": args,
            "reason": reason,
        }
        with self._lock:
            self._permissions[request_id] = (answer, event)
        self.emit(event)
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
            pending = self._permissions.get(request_id)
        if pending is None:
            return False
        answer, _ = pending
        try:
            answer.put_nowait(allowed)
        except queue.Full:
            return False
        return True


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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

            session_actions = {
                "/api/session/create": state.create_session,
                "/api/session/activate": state.activate_session,
                "/api/session/rename": state.rename_session,
                "/api/session/delete": state.delete_session,
            }
            if self.path in session_actions:
                try:
                    if self.path == "/api/session/create":
                        title = payload.get("title", "新会话")
                        if not isinstance(title, str):
                            raise ValueError("session title must be a string")
                        snapshot = state.create_session(title)
                    else:
                        session_id = payload.get("id")
                        if not isinstance(session_id, str) or not session_id:
                            raise ValueError("session id is required")
                        if self.path == "/api/session/rename":
                            title = payload.get("title")
                            if not isinstance(title, str):
                                raise ValueError("session title must be a string")
                            snapshot = state.rename_session(session_id, title)
                        else:
                            snapshot = session_actions[self.path](session_id)
                except RuntimeError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                except KeyError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, snapshot)
                return

            if self.path == "/api/stop":
                if not state.stop():
                    self._json(HTTPStatus.CONFLICT, {"error": "no active agent run"})
                    return
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
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
        workdir,
        verbose_tools=False,
        ask_user=state.ask_user,
        should_stop=state.cancelled,
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
