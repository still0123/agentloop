import json

import pytest

from agentloop.agent import Agent
from agentloop.artifacts import ToolOutput, read_artifact, wire_messages
from agentloop.budget import RequestBudget
from agentloop.compact import Compactor
from agentloop.hooks import HookRegistry
from agentloop.models import MockClient
from agentloop.tools import Toolbox, build_toolbox


def pair(ident, output):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": ident, "name": "read_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": ident,
                    "content": str(output),
                    **(
                        {"_artifact": output.artifact}
                        if isinstance(output, ToolOutput)
                        else {}
                    ),
                }
            ],
        },
    ]


def test_recall_and_recompact_ten_times_keeps_one_original(workdir):
    original = "1: original log\n" + "中文 evidence\n" * 500
    c = Compactor(workdir, char_limit=1800, keep_recent_results=0)
    path = c._save_output(original)
    box, _ = build_toolbox(workdir)
    for i in range(10):
        output = box.execute(
            {"name": "read_file", "input": {"path": path, "max_chars": 700}}
        )
        assert isinstance(output, ToolOutput)
        assert json.loads(output)["text"] == original[:700]
        messages = [
            {"role": "user", "content": "inspect"},
            *pair(str(i), output),
            *pair("new", "new observation" * 10),
        ]
        # Force eviction of the consumed read, while retaining the newest pair.
        c.char_limit = c._estimate(messages) - 200
        out = c.prepare(messages)
        assert (
            out[2]["content"][0]["content"] == f"[Earlier tool result saved at {path}]"
        )
        # Persist/restore history before the next compactor, as a real session does.
        out = json.loads(json.dumps(out))
        fresh = Compactor(workdir, char_limit=c.char_limit, keep_recent_results=0)
        assert fresh.prepare(out) == out
    assert len(list((workdir / ".task_outputs/tool-results").glob("*.txt"))) == 1
    assert (workdir / path).read_text() == original


def test_long_single_line_json_is_fully_recoverable_by_cursor(workdir):
    path = workdir / "Q1.json"
    original = json.dumps(
        {"log": "中文" * 7000, "final_field": "must survive"}, ensure_ascii=False
    )
    path.write_text(original)
    chunks, offset = [], 0
    while offset is not None:
        page = json.loads(read_artifact(workdir, path, offset, 1234))
        chunks.append(page["text"])
        offset = page["next_char_offset"]
    assert "".join(chunks) == original
    assert json.loads("".join(chunks))["final_field"] == "must survive"


def test_changed_or_outside_artifact_cannot_be_reused(workdir, tmp_path):
    path = workdir / "evidence.txt"
    path.write_text("first")
    output = read_artifact(workdir, path)
    path.write_text("changed")
    c = Compactor(workdir, char_limit=1000, keep_recent_results=0)
    with pytest.raises(ValueError, match="changed"):
        c._result_reference(pair("read", output)[1]["content"][0])
    with pytest.raises(ValueError, match="outside"):
        read_artifact(workdir, workdir.parent / "outside.txt")


def test_only_observed_artifact_range_enters_summary(workdir):
    path = workdir / "evidence.json"
    path.write_text("OBSERVED" + "x" * 1000 + "UNREAD_SECRET")
    output = read_artifact(workdir, path, 0, 8)
    rendered = Compactor(workdir)._render_for_summary(pair("read", output))
    assert "OBSERVED" in rendered
    assert "UNREAD_SECRET" not in rendered


def test_budget_preserves_older_results_when_the_request_fits(workdir):
    c = Compactor(workdir, request_budget=RequestBudget(100000, 1000))
    messages = [{"role": "user", "content": "inspect"}]
    for i in range(30):
        messages += pair(str(i), "evidence-" + str(i) + "x" * 1800)
    assert c.prepare(messages) == messages  # >50 messages and >50k chars
    assert not (workdir / ".task_outputs").exists()
    assert c.last_report.triggers == []


def test_explicit_character_limit_is_still_enforced(workdir):
    c = Compactor(workdir, request_budget=RequestBudget(100000, 1000), char_limit=3000)
    c.prepare([{"role": "user", "content": "inspect"}, *pair("large", "x" * 8000)])
    assert c.last_report.triggers == ["characters"]
    assert c.last_report.budget_satisfied


def test_live_state_and_provenance_survive_compaction_but_not_wire_fields(workdir):
    path = workdir / "evidence.json"
    path.write_text("observed result")
    state = {"queried": False}
    box = Toolbox()

    def observe():
        state["queried"] = True
        return read_artifact(workdir, path)

    box.add("observe", "Read evidence", {"type": "object", "properties": {}}, observe)
    model = MockClient([[("observe", {})], "done"])
    compactor = Compactor(
        workdir,
        client=MockClient(["Only nonexistent tools are available."]),
        char_limit=3000,
    )
    agent = Agent(
        model,
        box,
        HookRegistry(),
        compactor,
        "Use actual tools.",
        context_provider=lambda: json.dumps(state),
    )
    result = agent.run(
        "inspect", [{"role": "assistant", "content": "old history " * 1000}]
    )
    assert '"queried": false' in model.calls[0]["system"]
    assert '"queried": true' in model.calls[1]["system"]
    assert all("Active tools: observe" in call["system"] for call in model.calls)
    assert "_artifact" not in json.dumps(model.calls)
    assert "_artifact" in json.dumps(result.messages)
    assert "Current runtime state" not in json.dumps(result.messages)
    budget = RequestBudget(100000, 1000)
    assert budget.estimate(result.messages) == budget.estimate(
        wire_messages(result.messages)
    )
