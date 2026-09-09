"""预算、用户要求保留、增量检查点及原文采样的行为回归。"""

import json

import pytest
from helpers import make_agent

from agentloop.budget import ContextBudgetError, RequestBudget
from agentloop.compact import Compactor
from agentloop.models import MockClient, ModelError, ModelResponse


def pair(text, tid="t1"):
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": "bash",
                    "input": {"command": "inspect"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
        },
    ]


def state(messages):
    return json.loads(messages[0]["content"].split("\n", 1)[1])


def test_120k_latest_result_fits_without_summary_and_can_be_read_back(workdir):
    client = MockClient(["must not be called"])
    c = Compactor(workdir, client=client)
    text = "x" * 120000 + "\nFATAL tail evidence"
    out = c.prepare([{"role": "user", "content": "inspect"}, *pair(text)])
    content = out[-1]["content"][0]["content"]
    assert c.last_report.after_chars <= c.char_limit
    assert c.last_report.budget_satisfied
    assert "FATAL tail evidence" in content
    assert client.calls == []
    assert (workdir / content.split("Full output: ")[1]).read_text() == text


def test_full_request_budget_includes_system_tools_and_output_reserve(workdir):
    budget = RequestBudget(12000, 2000, 1000)
    c = Compactor(workdir, client=MockClient(["summary"]), request_budget=budget)
    system = "policy " * 100
    tools = [{"name": "bash", "description": "tool definition " * 100}]
    out = c.prepare(
        [{"role": "user", "content": "inspect"}, *pair("你" * 10000)],
        system_prompt=system,
        tools=tools,
    )
    assert budget.fits(out, system, tools)
    assert c.last_report.estimated_input_tokens == budget.estimate(out, system, tools)
    assert c.last_report.input_limit_tokens == 9000


def test_impossible_protected_request_fails_instead_of_sending_oversized_input(workdir):
    c = Compactor(workdir, char_limit=1000)
    with pytest.raises(ContextBudgetError):
        c.prepare([{"role": "user", "content": "keep this request " * 1000}])
    assert not c.last_report.budget_satisfied


def test_cannot_shrink_system_prompt_by_discarding_user_requirements(workdir):
    c = Compactor(workdir, request_budget=RequestBudget(1000, 100))
    with pytest.raises(ContextBudgetError):
        c.prepare(
            [{"role": "user", "content": "do not write"}], system_prompt="x" * 2000
        )


def test_reactive_compaction_reduces_oversized_recent_group(workdir):
    c = Compactor(workdir, client=MockClient(["brief"]))
    messages = [{"role": "user", "content": "inspect"}, *pair("x" * 120000)]
    out = c.reactive_compact(messages)
    assert len(json.dumps(out)) < len(json.dumps(messages)) // 2
    assert c.last_report.budget_satisfied
    # 若保留调用，结果仍需匹配该调用；否则二者一起归档。
    if len(out) > 1:
        assert out[-2]["content"][0]["id"] == out[-1]["content"][0]["tool_use_id"]


def test_prior_user_constraint_survives_continue_and_summary_omission(workdir):
    client = MockClient(["summary without constraints", "another summary"])
    c = Compactor(workdir, client=client)
    out = c.summarize(
        [
            {"role": "user", "content": "只检查，不写入", "_request_id": "req-1"},
            {"role": "assistant", "content": "OLD_HISTORY"},
        ]
    )
    out = c.summarize(
        [
            *out,
            {"role": "user", "content": "继续", "_request_id": "req-2"},
            {"role": "assistant", "content": "NEW_HISTORY"},
        ]
    )
    records = state(out)["user_requests"]
    assert records == [
        {"source_id": "req-1", "text": "只检查，不写入"},
        {"source_id": "req-2", "text": "继续"},
    ]
    second = json.loads(client.calls[1]["messages"][0]["content"])
    assert second["previous_summary"] == "summary without constraints"
    assert "NEW_HISTORY" in second["new_history"]
    assert "OLD_HISTORY" not in second["new_history"]
    assert state(out)["revision"] == 2


def test_retained_user_message_is_not_duplicated_in_checkpoint(workdir):
    c = Compactor(workdir, client=MockClient(["summary", "summary"]))
    messages = [{"role": "assistant", "content": f"history{i}"} for i in range(8)]
    messages += [{"role": "user", "content": "keep constraint", "_request_id": "req1"}]
    out = c.summarize(messages)
    out = c.summarize(out)
    assert state(out)["user_requests"] == [
        {"source_id": "req1", "text": "keep constraint"}
    ]


def test_failed_incremental_summary_retains_old_summary_and_replays_delta(workdir):
    class Flaky:
        def __init__(self):
            self.calls = []

        def complete(self, system, messages, tools):
            payload = json.loads(messages[0]["content"])
            self.calls.append(payload)
            if len(self.calls) == 2:
                raise ModelError("unavailable")
            return MockClient([f"summary-{len(self.calls)}"]).complete(
                system, messages, tools
            )

    client = Flaky()
    c = Compactor(workdir, client=client)
    out = c.summarize([{"role": "user", "content": "do not write"}])
    out = c.summarize([*out, {"role": "assistant", "content": "UNSUMMARIZED_EVIDENCE"}])
    failed = state(out)
    assert failed["summary"] == "summary-1"
    assert failed["revision"] == 1
    assert failed["pending_transcripts"]
    # 使用全新 Compactor，验证从会话 JSON 恢复而非依赖对象缓存。
    fresh = Compactor(workdir, client=client)
    recovered = fresh.summarize(out)
    assert "UNSUMMARIZED_EVIDENCE" in client.calls[2]["new_history"]
    assert state(recovered)["pending_transcripts"] == []
    assert state(recovered)["revision"] == 2


def test_summary_sees_middle_error_tail_and_tool_metadata(workdir):
    client = MockClient(["summary"])
    c = Compactor(workdir, client=client)
    output = (
        "normal\n" * 2000
        + "ERROR critical middle\n"
        + "normal\n" * 2000
        + "TAIL_RESULT"
    )
    c.summarize(
        [
            {"role": "user", "content": "inspect"},
            *pair(output),
            {"role": "assistant", "content": "finished reading"},
        ]
    )
    rendered = json.loads(client.calls[0]["messages"][0]["content"])["new_history"]
    assert "ERROR critical middle" in rendered
    assert "TAIL_RESULT" in rendered
    assert "bash" in rendered
    assert "inspect" in rendered


def test_summary_sampling_prioritizes_recent_history_under_cap(workdir):
    c = Compactor(workdir)
    messages = [{"role": "assistant", "content": "old text " * 10000}]
    messages.append({"role": "assistant", "content": "RECENT_ERROR"})
    rendered = c._render_for_summary(messages, cap=1000)
    assert "RECENT_ERROR" in rendered
    assert len(rendered) <= 1000


def test_agent_request_ids_persist_but_do_not_reach_provider(workdir):
    agent, client = make_agent(["done", "done"], workdir)
    first = agent.run("only inspect")
    assert first.messages[0]["_request_id"]
    second = agent.run("continue", first.messages)
    assert second.messages[-2]["_request_id"] != first.messages[0]["_request_id"]
    assert all(
        not any(key.startswith("_") for key in msg)
        for call in client.calls
        for msg in call["messages"]
    )


def test_framework_continuation_does_not_become_user_requirement(workdir):
    agent, client = make_agent(["first", "done"], workdir)
    agent.hooks.register(
        "UserPromptSubmit", lambda text: text + " [framework environment]"
    )
    agent.hooks.register(
        "Stop", lambda _: "INTERNAL_CONTINUE" if len(client.calls) == 1 else None
    )
    result = agent.run("only inspect")
    c = Compactor(workdir, client=MockClient(["summary"]))
    out = c.summarize(result.messages)
    assert [r["text"] for r in state(out)["user_requests"]] == ["only inspect"]
    assert state(out)["current_request"] == "only inspect"
    assert all(
        not any(key.startswith("_") for key in msg)
        for call in client.calls
        for msg in call["messages"]
    )


@pytest.mark.parametrize("reason", ["length", "max_tokens", "content_filter"])
def test_partial_summary_does_not_commit_checkpoint(workdir, reason):
    class Truncated:
        def complete(self, **kwargs):
            return ModelResponse(
                blocks=[{"type": "text", "text": "partial"}],
                text="partial",
                finish_reason=reason,
            )

    c = Compactor(workdir, client=MockClient(["previous reliable summary"]))
    first = c.summarize([{"role": "user", "content": "only inspect"}])
    c.client = Truncated()
    second = c.summarize([*first, {"role": "assistant", "content": "NEW_EVIDENCE"}])
    assert state(second)["summary"] == state(first)["summary"]
    assert state(second)["revision"] == state(first)["revision"]
    assert state(second)["summarized_messages"] == state(first)["summarized_messages"]
    assert state(second)["pending_transcripts"]
    client = MockClient(["recovered"])
    recovered = Compactor(workdir, client=client).summarize(second)
    assert "NEW_EVIDENCE" in client.calls[0]["messages"][0]["content"]
    assert state(recovered)["pending_transcripts"] == []


def test_summary_budget_keeps_unsent_messages_pending_across_restore(workdir):
    budget = RequestBudget(3200, 500, 200)
    client = MockClient(["brief"] * 10)
    c = Compactor(workdir, client=client, request_budget=budget)
    messages = [
        {"role": "assistant", "content": f"EVIDENCE_{i} " + "x" * 1400}
        for i in range(4)
    ]
    out = c.summarize(messages, current_request="inspect")
    assert 0 < state(out)["summarized_messages"] < len(messages)
    assert state(out)["pending_transcripts"]
    for _ in range(10):
        if not state(out)["pending_transcripts"]:
            break
        out = Compactor(workdir, client=client, request_budget=budget).summarize(out)
    assert state(out)["pending_transcripts"] == []
    assert state(out)["summarized_messages"] == len(messages)
    sent = "\n".join(call["messages"][0]["content"] for call in client.calls)
    for i in range(4):
        assert sent.count(f"EVIDENCE_{i}") == 1
    assert all(
        budget.fits(call["messages"], call["system"], call["tools"])
        for call in client.calls
    )


def test_summary_input_too_small_keeps_history_without_empty_model_call(workdir):
    client = MockClient(["must not be called"])
    c = Compactor(workdir, client=client, request_budget=RequestBudget(600, 500))
    with pytest.raises(ContextBudgetError):
        c.summarize([{"role": "assistant", "content": "evidence " * 1000}])
    assert not client.calls
    assert c.last_state.pending_transcripts


def test_many_user_requests_stop_before_any_requirement_is_dropped(workdir):
    c = Compactor(workdir, char_limit=2000)
    messages = [
        {"role": "user", "content": f"constraint {i}", "_request_id": str(i)}
        for i in range(100)
    ]
    with pytest.raises(ContextBudgetError):
        c.prepare(messages)
    assert len(c.last_state.user_requests) == 100
