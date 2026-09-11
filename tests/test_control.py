import pytest
from helpers import make_agent, tool_results

from agentloop.control import CallBudget, RoundControl


def test_budget_uses_absolute_deadline():
    now = [0]
    budget = CallBudget(60, clock=lambda: now[0])
    for tick in range(60):
        now[0] = tick
        assert not budget.expired()
    now[0] = 60
    assert budget.expired()


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True])
def test_invalid_budget(seconds):
    with pytest.raises(ValueError):
        CallBudget(seconds)


def test_core_rejects_registered_tool_absent_from_request_snapshot(workdir):
    agent, raw = make_agent(
        [[("write_file", {"path": "unexpected", "content": "x"})]], workdir, max_turns=1
    )
    raw.round_control = RoundControl.from_tools([{"name": "read_file"}])
    result = agent.run("read only")
    assert not (workdir / "unexpected").exists()
    assert "not available this round" in tool_results(result.messages)[0]


def test_separated_recalls_are_not_a_consecutive_loop(workdir):
    (workdir / "a").write_text("same evidence")
    (workdir / "b").write_text("other evidence")
    turns = [[("read_file", {"path": path})] for path in ["a", "b", "a", "b", "a"]]
    agent, _ = make_agent(turns, workdir, max_turns=5)
    events = []
    agent.run("compare evidence", on_event=events.append)
    assert not any(event["type"] == "loop_warning" for event in events)
