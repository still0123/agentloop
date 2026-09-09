"""验证压缩后的信息恢复、失败降级与真实 Agent 循环接回。"""

import copy
import json

import pytest
from helpers import make_agent

from agentloop.compact import Compactor
from agentloop.models import MockClient, ModelCancelled, ModelError
from agentloop.tools import build_toolbox


def pair(tid, text):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tid, "name": "read_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
        },
    ]


class FailedSummary:
    def complete(self, *args, **kwargs):
        raise ModelError("provider unavailable")


def test_old_results_remain_retrievable_without_mutating_session(workdir):
    messages = [{"role": "user", "content": "diagnose"}]
    for i in range(5):
        messages += pair(str(i), f"evidence-{i}:" + "x" * 500)
    original = copy.deepcopy(messages)
    out = Compactor(workdir, keep_recent_results=1).prepare(messages)
    marker = out[2]["content"][0]["content"]
    path = marker.removeprefix("[Earlier tool result saved at ").removesuffix("]")
    toolbox, _ = build_toolbox(workdir)
    assert toolbox.execute({"name": "read_file", "input": {"path": path}}) == (
        "evidence-0:" + "x" * 500
    )
    assert messages == original
    assert out[-1] == original[-1]


@pytest.mark.parametrize("consumed", [0, 1, 2, 3])
def test_recent_result_retention_does_not_use_negative_slice(workdir, consumed):
    messages = [{"role": "user", "content": "q"}]
    for i in range(consumed + 1):
        messages += pair(str(i), "x" * 500)
    assert Compactor(workdir, keep_recent_results=3).prepare(messages) == messages


def test_spill_paths_ignore_untrusted_ids_and_do_not_overwrite(workdir):
    c = Compactor(workdir, batch_budget=100, spill_threshold=100, spill_preview=20)
    paths = []
    for value in ["a" * 1000, "b" * 1000]:
        out = c.prepare(pair("../../escape", value))
        path = out[-1]["content"][0]["content"].split("Full output: ")[1]
        paths.append(path)
        assert (workdir / path).resolve().is_relative_to(workdir.resolve())
    assert paths[0] != paths[1]
    assert (workdir / paths[0]).read_text() == "a" * 1000


@pytest.mark.parametrize("client", [None, FailedSummary(), MockClient([""])])
def test_summary_failure_keeps_request_archive_and_tool_pair(workdir, client):
    messages = [{"role": "user", "content": "old goal"}]
    messages += [{"role": "assistant", "content": "old context" * 1000}]
    messages += pair("pending", "new observation")
    c = Compactor(workdir, client=client, char_limit=1000)
    out = c.prepare(messages, current_request="Only inspect, do not edit")
    state = json.loads(out[0]["content"].split("\n", 1)[1])
    assert state["current_request"] == "Only inspect, do not edit"
    assert state["mode"] == "fallback"
    assert json.loads((workdir / state["transcript"]).read_text()) == messages
    assert out[-2:] == messages[-2:]
    assert c.last_report.fallback


def test_repeated_compaction_preserves_explicit_request(workdir):
    c = Compactor(workdir, client=MockClient(["first", "second"]), char_limit=2000)
    messages = [{"role": "user", "content": "old task"}]
    out = c.summarize(messages, current_request="new task; no writes")
    out = c.summarize(out)
    assert json.loads(out[0]["content"].split("\n", 1)[1])["current_request"] == (
        "new task; no writes"
    )


def test_model_cancellation_is_not_swallowed(workdir):
    class Cancelled:
        def complete(self, *args, **kwargs):
            raise ModelCancelled("cancelled")

    with pytest.raises(ModelCancelled):
        Compactor(workdir, client=Cancelled()).summarize(
            [{"role": "user", "content": "q"}]
        )


def test_agent_continues_with_tool_after_summary_failure(workdir):
    (workdir / "evidence.txt").write_text("verified finding")
    agent, model = make_agent(
        [[("read_file", {"path": "evidence.txt"})], "done"],
        workdir,
        compactor_kwargs={"char_limit": 2000},
    )
    agent.compactor.client = FailedSummary()
    result = agent.run(
        "inspect evidence only",
        [
            {"role": "user", "content": "old goal"},
            {"role": "assistant", "content": "old log" * 1000},
        ],
    )
    assert result.text == "done"
    state = json.loads(model.calls[0]["messages"][0]["content"].split("\n", 1)[1])
    assert state["mode"] == "fallback"
    assert state["current_request"] == "inspect evidence only"
    assert "verified finding" in json.dumps(model.calls[1]["messages"])


def test_repeated_placeholder_keeps_original_evidence_path(workdir):
    c = Compactor(workdir, keep_recent_results=0)
    original = "unique evidence" * 100
    messages = [{"role": "user", "content": "q"}, *pair("old", original)]
    messages += pair("new", "recent")
    once = c.prepare(messages)
    twice = c.prepare(once)
    assert twice == once
    assert len(list((workdir / ".task_outputs/tool-results").glob("*.txt"))) == 1


def test_summary_usage_is_recorded_separately(workdir):
    c = Compactor(workdir, client=MockClient(["summary"]))
    out = c.summarize([{"role": "user", "content": "request"}])
    assert c.last_report.summary_calls == 1
    assert c.last_report.after_chars == len(json.dumps(out, ensure_ascii=False))
    assert c.last_report.summary_input_tokens == 10
    assert c.last_report.summary_output_tokens == 5


def test_oversized_summary_degrades_without_losing_request(workdir):
    c = Compactor(workdir, client=MockClient(["x" * 9000]))
    out = c.summarize([{"role": "user", "content": "do not deploy"}])
    state = json.loads(out[0]["content"].split("\n", 1)[1])
    assert state["mode"] == "fallback"
    assert state["current_request"] == "do not deploy"


def test_program_error_is_not_disguised_as_model_fallback(workdir):
    class Broken:
        def complete(self, *args, **kwargs):
            raise TypeError("implementation bug")

    with pytest.raises(TypeError, match="implementation bug"):
        Compactor(workdir, client=Broken()).summarize(
            [{"role": "user", "content": "q"}]
        )


def test_reactive_compaction_rejoins_loop_with_current_request(workdir):
    agent, _ = make_agent(["unused"], workdir)

    class RejectOnce:
        def __init__(self):
            self.calls = 0
            self.accepted = None

        def complete(self, system, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise ModelError("context length exceeded")
            self.accepted = messages
            return MockClient(["resumed"]).complete(system, messages, tools)

    model = RejectOnce()
    agent.client = model
    result = agent.run("current request", [{"role": "user", "content": "old"}])
    assert result.text == "resumed"
    assert model.calls == 2
    state = json.loads(model.accepted[0]["content"].split("\n", 1)[1])
    assert state["current_request"] == "current request"


def test_history_snip_keeps_current_request_before_summary_threshold(workdir):
    messages = [{"role": "user", "content": "old goal"}]
    messages += [{"role": "assistant", "content": "history"}] * 5
    messages += [{"role": "user", "content": "ONLY_INSPECT"}]
    messages += [{"role": "assistant", "content": "working"}] * 55
    c = Compactor(workdir, char_limit=1_000_000)
    out = c.prepare(messages, current_request="ONLY_INSPECT")
    assert "ONLY_INSPECT" in json.dumps(out)
    assert c.last_report.summary_calls == 0
