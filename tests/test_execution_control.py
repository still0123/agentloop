from helpers import make_agent, tool_results


def test_last_turn_executes_requested_tool_and_keeps_pairs(workdir):
    agent, mock = make_agent(
        [[("write_file", {"path": "report.txt", "content": "evidence"})]],
        workdir,
        max_turns=1,
    )
    result = agent.run("produce report")
    assert (workdir / "report.txt").read_text() == "evidence"
    assert len(tool_results(result.messages)) == 1
    assert len(mock.calls) == 1
    assert result.stopped_reason == "max_turns"


def test_stop_hook_cannot_bypass_turn_budget(workdir):
    agent, mock = make_agent(["not a valid report"] * 5, workdir, max_turns=3)
    agent.hooks.register("Stop", lambda messages: "Submit a valid report")
    result = agent.run("work")
    assert len(mock.calls) == result.turns == 3
    assert result.stopped_reason == "max_turns"


def test_completion_is_not_cancellation_and_skips_remaining_tools(workdir):
    done = []
    agent, mock = make_agent(
        [[("submit", {}), ("write_file", {"path": "unexpected", "content": "x"})]],
        workdir,
        max_turns=1,
        is_complete=lambda: bool(done),
    )
    agent.toolbox.add("submit", "submit", {}, lambda: done.append(True) or "accepted")
    result = agent.run("complete task")
    assert result.stopped_reason == "done"
    assert len(mock.calls) == 1
    assert not (workdir / "unexpected").exists()
    assert len(tool_results(result.messages)) == 2
    assert "not executed" in tool_results(result.messages)[1]


def test_finalization_reserves_validation_retry(workdir):
    done = []
    agent, mock = make_agent(
        [
            [("read_file", {"path": "input.txt"})],
            [("submit", {"valid": False})],
            [("submit", {"valid": True})],
        ],
        workdir,
        max_turns=3,
        is_complete=lambda: bool(done),
    )
    (workdir / "input.txt").write_text("source evidence")

    def submit(valid):
        if not valid:
            raise ValueError("missing evidence reference")
        done.append(True)
        return "accepted"

    agent.toolbox.add("submit", "submit", {}, submit)
    agent.finalize_turns, agent.finalize_tools = 2, ("submit",)
    events = []
    result = agent.run("investigate", on_event=events.append)
    assert result.stopped_reason == "done"
    assert len(mock.calls) == 3
    assert [t["name"] for t in mock.calls[1]["tools"]] == ["submit"]
    assert "missing evidence reference" in str(mock.calls[2]["messages"])
    assert len([e for e in events if e["type"] == "finalization_start"]) == 1


def test_finalization_rejects_unoffered_tool_even_if_model_requests_it(workdir):
    agent, mock = make_agent(
        [
            [("read_file", {"path": "absent"})],
            [("write_file", {"path": "must-not-exist", "content": "x"})],
            "gaps",
        ],
        workdir,
        max_turns=3,
        finalize_turns=2,
    )
    result = agent.run("inspect")
    assert not (workdir / "must-not-exist").exists()
    assert "Finalization only" in str(tool_results(result.messages))
    assert mock.calls[1]["tools"] == []
    assert result.text == "gaps"


def test_deadline_stops_between_tools_and_preserves_pairing(workdir, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("agentloop.agent.time.monotonic", lambda: now[0])
    agent, mock = make_agent(
        [[("slow", {}), ("write_file", {"path": "too-late", "content": "x"})]],
        workdir,
        max_seconds=10,
    )
    agent.toolbox.add("slow", "slow", {}, lambda: now.__setitem__(0, 11) or "partial")
    result = agent.run("bounded work")
    assert result.stopped_reason == "max_time"
    assert len(mock.calls) == 1
    assert not (workdir / "too-late").exists()
    assert len(tool_results(result.messages)) == 2


def test_deadline_reserves_a_final_response(workdir, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("agentloop.agent.time.monotonic", lambda: now[0])
    agent, mock = make_agent(
        [[("slow", {})], "observations and gaps"],
        workdir,
        max_seconds=10,
        finalize_seconds=3,
    )
    agent.toolbox.add("slow", "slow", {}, lambda: now.__setitem__(0, 8) or "partial")
    result = agent.run("bounded work")
    assert result.text == "observations and gaps"
    assert mock.calls[-1]["tools"] == []


def test_repetition_warns_without_caching_or_blocking(workdir):
    calls = []
    agent, _ = make_agent([[("poll", {})]] * 4 + ["gaps"], workdir)
    agent.toolbox.add("poll", "poll", {}, lambda: calls.append(True) or "unchanged")
    events = []
    agent.run("poll", on_event=events.append)
    assert len(calls) == 4
    assert len([e for e in events if e["type"] == "loop_warning"]) == 1


def test_changed_observations_are_not_repetition(workdir):
    calls = []
    agent, _ = make_agent([[("poll", {})]] * 4 + ["done"], workdir)
    agent.toolbox.add("poll", "poll", {}, lambda: calls.append(True) or str(len(calls)))
    events = []
    agent.run("poll", on_event=events.append)
    assert not any(e["type"] == "loop_warning" for e in events)
