"""模型路由与内部消息适配测试，全程不访问网络。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentloop.agent import _is_prompt_too_long
from agentloop.models import (
    AnthropicClient,
    FallbackClient,
    MockClient,
    ModelCancelled,
    ModelError,
    OpenAICompatClient,
    _openai_wire_messages,
    build_client,
    detect_provider,
)


def _serve_sse(chunks):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.server.payload = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                data = chunk if isinstance(chunk, str) else json.dumps(chunk)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_detect_provider_mapping():
    expected = {
        "glm-4.6": "glm",
        "chatglm-3": "glm",
        "deepseek-chat": "deepseek",
        "qwen-max": "qwen",
        "qwq-32b": "qwen",
        "moonshot-v1": "moonshot",
        "kimi-k2": "moonshot",
        "claude-sonnet-4-5": "anthropic",
        "gpt-4o": "openai",
        "o3-mini": "openai",
        "my-own-model": None,
    }
    assert {model: detect_provider(model) for model in expected} == expected


def test_openai_wire_messages_conversion():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "t"},
                {
                    "type": "tool_use",
                    "id": "1",
                    "name": "bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "out"},
                {"type": "text", "text": "<reminder>x</reminder>"},
            ],
        },
    ]

    wire = _openai_wire_messages("S", messages)

    assert wire[0] == {"role": "system", "content": "S"}
    assert wire[1] == {"role": "user", "content": "hi"}
    assert wire[2]["role"] == "assistant"
    assert wire[2]["content"] == "t"
    assert wire[2]["tool_calls"][0]["id"] == "1"
    assert wire[2]["tool_calls"][0]["function"]["name"] == "bash"
    assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {
        "command": "ls"
    }
    assert wire[3] == {"role": "tool", "tool_call_id": "1", "content": "out"}
    assert wire[4] == {"role": "user", "content": "<reminder>x</reminder>"}


def test_mock_client_usage_and_exhaustion():
    client = MockClient(["a"])
    deltas = []

    response = client.complete(
        "system",
        [{"role": "user", "content": "q"}],
        [],
        on_text=deltas.append,
    )
    exhausted = client.complete("system", [], [{"name": "bash"}])

    assert response.text == "a"
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}
    assert response.blocks == [{"type": "text", "text": "a"}]
    assert deltas == ["a"]
    assert exhausted.text == "(mock script exhausted)"
    assert client.calls[0] == {
        "system": "system",
        "messages": [{"role": "user", "content": "q"}],
        "tools": [],
    }
    assert client.calls[1]["tools"] == [{"name": "bash"}]


def test_fallback_client_switches():
    class Boom:
        model = "boom"

        def complete(self, system, messages, tools):
            raise ModelError("down")

    ok = MockClient(["served"])
    fallback = FallbackClient([Boom(), ok])

    assert fallback.complete("s", [], []).text == "served"
    assert fallback.switched_to == ["mock"]
    with pytest.raises(ModelError):
        FallbackClient([Boom(), Boom()]).complete("s", [], [])


def test_fallback_does_not_continue_after_cancellation():
    class Cancelled:
        model = "cancelled"

        def complete(self, system, messages, tools):
            raise ModelCancelled("cancelled")

    fallback = MockClient(["should not run"])
    with pytest.raises(ModelCancelled):
        FallbackClient([Cancelled(), fallback]).complete("s", [], [])
    assert fallback.calls == []


def test_fallback_does_not_continue_after_partial_stream():
    class Partial:
        model = "partial"

        def complete(self, system, messages, tools, on_text=None):
            on_text("partial")
            raise ModelError("stream failed")

    fallback = MockClient(["should not run"])
    with pytest.raises(ModelError, match="stream failed"):
        FallbackClient([Partial(), fallback]).complete(
            "s", [], [], on_text=lambda delta: None
        )
    assert fallback.calls == []


def test_build_client_env_injection():
    def should_stop():
        return False

    glm = build_client("glm-4.6", env={"GLM_API_KEY": "k"}, should_stop=should_stop)
    assert isinstance(glm, OpenAICompatClient)
    assert "open.bigmodel.cn" in glm.base_url
    assert glm.api_key == "k"
    assert glm.should_stop is should_stop

    anthropic = build_client(
        "claude-x", env={"ANTHROPIC_API_KEY": "k"}, should_stop=should_stop
    )
    assert isinstance(anthropic, AnthropicClient)
    assert anthropic.should_stop is should_stop

    local = build_client(
        "llama3", env={"AGENTLOOP_BASE_URL": "http://localhost:11434/v1"}
    )
    assert isinstance(local, OpenAICompatClient)
    assert local.base_url == "http://localhost:11434/v1"
    assert local.api_key == "EMPTY"

    with pytest.raises(SystemExit, match="GLM_API_KEY"):
        build_client("glm-4.6", env={})
    with pytest.raises(SystemExit, match="AGENTLOOP_MODEL"):
        build_client(None, env={})

    explicit = build_client(
        "x",
        env={"AGENTLOOP_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
    )
    assert isinstance(explicit, AnthropicClient)


def test_openai_http_request_can_be_cancelled():
    started = threading.Event()
    cancelled = threading.Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            started.set()
            time.sleep(2)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            return

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            return

    server = Server(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cancel_thread = threading.Thread(
        target=lambda: started.wait(1) and cancelled.set(), daemon=True
    )
    cancel_thread.start()
    client = OpenAICompatClient(
        "test",
        f"http://127.0.0.1:{server.server_port}",
        "key",
        timeout=10,
        retries=1,
        should_stop=cancelled.is_set,
    )
    began = time.monotonic()

    try:
        with pytest.raises(ModelCancelled):
            client.complete(
                "system",
                [{"role": "user", "content": "hi"}],
                [],
                on_text=lambda delta: None,
            )
        assert time.monotonic() - began < 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        cancel_thread.join(timeout=2)


def test_openai_stream_rebuilds_text_tools_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command":',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"echo hi"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
        "[DONE]",
    ]
    server, thread = _serve_sse(chunks)
    deltas = []
    client = OpenAICompatClient(
        "test", f"http://127.0.0.1:{server.server_port}", "key", retries=1
    )

    try:
        response = client.complete("system", [], [], on_text=deltas.append)
        assert deltas == ["Hel", "lo"]
        assert response.text == "Hello"
        assert response.blocks[1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "bash",
            "input": {"command": "echo hi"},
        }
        assert response.usage == {"input_tokens": 3, "output_tokens": 2}
        assert server.payload["stream"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_openai_stream_error_event_fails():
    server, thread = _serve_sse([{"error": {"message": "provider failed"}}])
    client = OpenAICompatClient(
        "test", f"http://127.0.0.1:{server.server_port}", "key", retries=1
    )

    try:
        with pytest.raises(ModelError, match="provider failed"):
            client.complete("system", [], [], on_text=lambda delta: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_anthropic_stream_rebuilds_text_tools_and_usage():
    chunks = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 4, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hi"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "!"},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "tool_1",
                "name": "read_file",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"path":"README.md"}',
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 5}},
    ]
    server, thread = _serve_sse(chunks)
    deltas = []
    client = AnthropicClient(
        "test",
        "key",
        base_url=f"http://127.0.0.1:{server.server_port}",
        retries=1,
    )

    try:
        response = client.complete("system", [], [], on_text=deltas.append)
        assert deltas == ["Hi", "!"]
        assert response.text == "Hi!"
        assert response.blocks[1] == {
            "type": "tool_use",
            "id": "tool_1",
            "name": "read_file",
            "input": {"path": "README.md"},
        }
        assert response.usage == {"input_tokens": 4, "output_tokens": 5}
        assert server.payload["stream"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "message",
    ["prompt_too_long", "too many tokens", "context length exceeded"],
)
def test_prompt_too_long_detection(message):
    assert _is_prompt_too_long(RuntimeError(message))
    assert not _is_prompt_too_long(RuntimeError("ordinary failure"))
