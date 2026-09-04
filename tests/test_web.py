import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from helpers import make_agent

from agentloop import cli
from agentloop.web import SessionStore, WebState, make_server


def _post(base_url, token, path, payload):
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-AgentLoop-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def _get(base_url, token, path):
    request = urllib.request.Request(
        base_url + path,
        headers={"X-AgentLoop-Token": token},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def _next(events, event_type):
    while True:
        event = events.get(timeout=2)
        if event["type"] == event_type:
            return event


def test_web_permission_approval_resumes_agent(workdir):
    target = workdir / "notes.txt"
    target.write_text("delete me")
    state = WebState(permission_timeout=2)
    state.agent, _ = make_agent(
        [[("bash", {"command": "rm notes.txt"})], "done"],
        workdir,
        ask=state.ask_user,
    )
    events = state.subscribe()
    token = "test-token"
    server = make_server(state, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        status, body = _post(base_url, token, "/api/run", {"prompt": "delete notes"})
        assert status == 202
        assert body == {"ok": True}

        request = _next(events, "permission_request")
        assert request["input"] == {"command": "rm notes.txt"}
        assert target.exists()
        _, snapshot = _get(base_url, token, "/api/session")
        assert snapshot["permissions"] == [request]

        status, body = _post(
            base_url,
            token,
            f"/api/permissions/{request['id']}",
            {"allowed": True},
        )
        assert status == 200
        assert body == {"ok": True}
        assert _next(events, "done")["text"] == "done"
        assert not target.exists()
        _, snapshot = _get(base_url, token, "/api/session")
        assert snapshot["permissions"] == []
    finally:
        state.unsubscribe(events)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_stop_cancels_running_bash(workdir):
    state = WebState()
    state.agent, _ = make_agent(
        [[("bash", {"command": "sleep 10"})], "never reached"],
        workdir,
        should_stop=state.cancelled,
    )
    events = state.subscribe()
    token = "test-token"
    server = make_server(state, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        assert _post(base_url, token, "/api/run", {"prompt": "wait"})[0] == 202
        assert _next(events, "tool_call")["name"] == "bash"
        assert _post(base_url, token, "/api/stop", {}) == (202, {"ok": True})
        assert _next(events, "tool_result")["cancelled"] is True
        done = _next(events, "done")
        assert done["stopped_reason"] == "cancelled"
        assert state.snapshot()["active"] is False
    finally:
        state.unsubscribe(events)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_stop_releases_permission_wait(workdir):
    state = WebState(permission_timeout=10)
    state.agent, _ = make_agent(
        [[("bash", {"command": "rm nonexistent"})], "never reached"],
        workdir,
        ask=state.ask_user,
        should_stop=state.cancelled,
    )
    events = state.subscribe()
    token = "test-token"
    server = make_server(state, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        assert _post(base_url, token, "/api/run", {"prompt": "delete"})[0] == 202
        _next(events, "permission_request")
        assert _post(base_url, token, "/api/stop", {}) == (202, {"ok": True})
        assert _next(events, "done")["stopped_reason"] == "cancelled"
        assert state.snapshot()["permissions"] == []
    finally:
        state.unsubscribe(events)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_api_rejects_missing_token():
    state = WebState()
    server = make_server(state, "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/api/run",
        data=b'{"prompt":"hi"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        assert caught.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_suppresses_expected_disconnect_errors(monkeypatch):
    server = make_server(WebState(), "secret")
    unexpected = []
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "handle_error",
        lambda *args: unexpected.append(True),
    )
    try:
        try:
            raise ConnectionResetError
        except ConnectionResetError:
            server.handle_error(None, None)
        assert unexpected == []

        try:
            raise RuntimeError
        except RuntimeError:
            server.handle_error(None, None)
        assert unexpected == [True]
    finally:
        server.server_close()


def test_web_permission_timeout_denies_by_default():
    state = WebState(permission_timeout=0.01)
    assert state.ask_user("bash", {"command": "rm file"}, "risky") is False


def test_session_store_roundtrip_and_clear(workdir):
    store = SessionStore(workdir, root=workdir / "state")
    messages = [{"role": "user", "content": "remember me"}]
    events = [{"type": "run_start", "prompt": "remember me"}]

    store.save(messages, events)

    assert store.load() == (messages, events)
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.clear()
    assert store.load() == ([], [])


def test_web_state_restores_completed_session(workdir):
    store = SessionStore(workdir, root=workdir / "state")
    state = WebState(store=store)
    state.agent, _ = make_agent(["persisted answer"], workdir)
    events = state.subscribe()

    assert state.start("remember this")
    assert _next(events, "done")["text"] == "persisted answer"
    assert state.snapshot()["active"] is False
    assert store.path.is_file()

    restored = WebState(store=store)
    assert restored.messages == state.messages
    assert [event["type"] for event in restored.events] == [
        "run_start",
        "assistant_message",
        "done",
    ]


def test_session_api_reads_and_clears_history(workdir):
    store = SessionStore(workdir, root=workdir / "state")
    store.save(
        [{"role": "user", "content": "old prompt"}],
        [{"type": "run_start", "prompt": "old prompt"}],
    )
    state = WebState(store=store)
    token = "test-token"
    server = make_server(state, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        status, body = _get(base_url, token, "/api/session")
        assert status == 200
        assert body["events"] == [{"type": "run_start", "prompt": "old prompt"}]

        status, body = _post(base_url, token, "/api/session/reset", {})
        assert status == 200
        assert body == {"ok": True}
        assert state.snapshot()["events"] == []
        assert not store.path.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_dispatches_web_subcommand(monkeypatch):
    import agentloop.web

    seen = []
    monkeypatch.setattr(agentloop.web, "main", lambda argv: seen.append(argv) or 7)

    assert cli.main(["web", "--no-open"]) == 7
    assert seen == [["--no-open"]]
