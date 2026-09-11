from helpers import make_agent

from agentloop.models import OpenAICompatClient, _openai_wire_messages


def test_streamed_reasoning_is_kept_but_not_emitted_as_answer(monkeypatch):
    def stream(url, payload, headers, timeout, should_stop, on_data):
        on_data({"choices": [{"delta": {"reasoning_content": "opaque-"}}]})
        on_data({"choices": [{"delta": {"reasoning_content": "state"}}]})
        on_data(
            {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("agentloop.models._stream_sse", stream)
    emitted = []
    response = OpenAICompatClient("test", "http://example.invalid", "key")._stream(
        {}, {}, emitted.append
    )
    assert response.reasoning_content == "opaque-state"
    assert response.text == "answer"
    assert emitted == ["answer"]
    wire = _openai_wire_messages(
        "system",
        [
            {
                "role": "assistant",
                "content": response.blocks,
                "reasoning_content": response.reasoning_content,
            }
        ],
    )
    assert wire[-1]["reasoning_content"] == "opaque-state"
    assert wire[-1]["content"] == "answer"


def test_plain_reasoning_and_plain_assistant_turn_are_preserved():
    response = OpenAICompatClient._parse(
        {"choices": [{"message": {"content": "answer", "reasoning_content": ""}}]}
    )
    assert response.reasoning_content == ""
    wire = _openai_wire_messages(
        "", [{"role": "assistant", "content": "answer", "reasoning_content": "opaque"}]
    )
    assert wire[-1]["reasoning_content"] == "opaque"


def test_agent_keeps_provider_state_across_tool_roundtrip(workdir):
    agent, mock = make_agent([[("read_file", {"path": "a.txt"})], "done"], workdir)
    (workdir / "a.txt").write_text("observed")
    complete = mock.complete

    def with_reasoning(*args, **kwargs):
        response = complete(*args, **kwargs)
        response.reasoning_content = "opaque provider state"
        return response

    mock.complete = with_reasoning
    result = agent.run("read a.txt")
    assistant = next(m for m in mock.calls[1]["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "opaque provider state"
    assert (
        _openai_wire_messages("", [assistant])[-1]["reasoning_content"]
        == "opaque provider state"
    )
    assert result.text == "done"


def test_thinking_payload_can_omit_unsupported_tool_choice(monkeypatch):
    def stream(url, payload, headers, timeout, should_stop, on_data):
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "low"
        assert "tool_choice" not in payload
        assert payload["tools"]
        on_data({"choices": [{"delta": {"content": "answer"}}]})

    monkeypatch.setattr("agentloop.models._stream_sse", stream)
    client = OpenAICompatClient(
        "test",
        "http://example.invalid",
        "key",
        tool_choice=None,
        reasoning_effort="low",
    )
    assert (
        client.complete("", [], [{"name": "query_logs"}], on_text=lambda _: None).text
        == "answer"
    )
