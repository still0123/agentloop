import pytest
from helpers import make_agent, tool_results


def test_final_text_without_tools(workdir):
    agent, mock = make_agent(["hello"], workdir)
    result = agent.run("hi")
    assert result.text == "hello"
    assert result.turns == 1
    assert len(mock.calls) == 1


def test_tool_roundtrip(workdir):
    agent, mock = make_agent([[("bash", {"command": "echo hi"})], "done"], workdir)
    result = agent.run("run echo")
    assert result.text == "done"
    assert len(mock.calls) == 2
    # 第二次调用模型时，消息尾部应包含执行结果
    second = mock.calls[1]["messages"]
    assert any("hi" in r for r in tool_results(second))


def test_multiple_tool_calls_execute_in_order(workdir):
    agent, _ = make_agent(
        [
            [
                ("write_file", {"path": "a.txt", "content": "A"}),
                ("write_file", {"path": "b.txt", "content": "B"}),
            ],
            "done",
        ],
        workdir,
    )
    agent.run("write two files")
    assert (workdir / "a.txt").read_text() == "A"
    assert (workdir / "b.txt").read_text() == "B"


def test_unknown_tool_returns_error_not_crash(workdir):
    agent, _ = make_agent([[("nope", {})], "handled"], workdir)
    result = agent.run("call weird tool")
    assert "unknown tool" in tool_results(result.messages)[0]
    assert result.text == "handled"


def test_todo_reminder_after_three_gap_rounds(workdir):
    batches = [[("bash", {"command": f"echo {i}"})] for i in range(4)]
    agent, _ = make_agent([*batches, "done"], workdir)
    result = agent.run("busy work without planning")
    texts = [
        b.get("text", "")
        for m in result.messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "text"
    ]
    assert any("<reminder>Update your todos.</reminder>" in t for t in texts)


def test_todo_usage_resets_reminder(workdir):
    bash = lambda i: [("bash", {"command": f"echo {i}"})]  # noqa: E731
    todo = [("todo_write", {"todos": [{"content": "s", "status": "in_progress"}]})]
    batches = [bash(1), bash(2), bash(3), todo, bash(4), bash(5), bash(6)]
    # 第 3 轮 gap=3 → 提醒①并清零；todo_write 本身也清零；
    # 再过 3 轮（4/5/6）gap=3 → 提醒②
    agent, _ = make_agent([*batches, "done"], workdir)
    result = agent.run("work")
    reminder_count = sum(
        1
        for m in result.messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "text" and "<reminder>" in b.get("text", "")
    )
    assert reminder_count == 2


def test_usage_accumulates(workdir):
    agent, _ = make_agent([[("bash", {"command": "echo x"})], "done"], workdir)
    result = agent.run("q")
    assert result.usage["input_tokens"] == 20  # MockClient 每次固定 10
    assert result.usage["output_tokens"] == 10


def test_max_turns_guard(workdir):
    batches = [[("bash", {"command": "echo x"})] for _ in range(5)]
    agent, _ = make_agent([*batches, "never reached"], workdir, max_turns=2)
    result = agent.run("loop forever")
    assert result.stopped_reason == "max_turns"
    assert "max_turns" in result.text


def test_session_continuity(workdir):
    agent, mock = make_agent(["first done", "second done"], workdir)
    first = agent.run("question one")
    second = agent.run("question two", first.messages)
    # 第二问的模型调用应看到第一问的完整历史
    assert len(mock.calls[1]["messages"]) > len(mock.calls[0]["messages"])
    assert second.messages[:2] == first.messages[:2]


def test_event_callback_reports_tool_roundtrip(workdir):
    events = []
    agent, _ = make_agent([[("bash", {"command": "echo hi"})], "done"], workdir)

    result = agent.run("run echo", on_event=events.append)

    assert [event["type"] for event in events] == [
        "run_start",
        "model_start",
        "tool_call",
        "tool_result",
        "model_start",
        "assistant_message",
        "done",
    ]
    assert events[2]["input"] == {"command": "echo hi"}
    assert "exit=0" in events[3]["content"]
    assert events[-1]["usage"] == result.usage


def test_event_callback_reports_errors(workdir):
    events = []
    agent, mock = make_agent(["unused"], workdir)

    def fail(*args):
        raise RuntimeError("model unavailable")

    mock.complete = fail
    with pytest.raises(RuntimeError, match="model unavailable"):
        agent.run("hi", on_event=events.append)

    assert events[-1] == {
        "type": "error",
        "error": "RuntimeError",
        "message": "model unavailable",
    }
