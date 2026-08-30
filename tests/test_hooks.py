from helpers import make_agent, tool_results


def test_pre_tool_use_blocks_execution(workdir):
    agent, _ = make_agent(
        [[("write_file", {"path": "side.txt", "content": "x"})], "done"],
        workdir,
    )
    agent.hooks.register("PreToolUse", lambda block: "blocked by test")
    result = agent.run("write the file")
    assert (workdir / "side.txt").exists() is False
    assert "blocked by test" in tool_results(result.messages)[0]


def test_stop_hook_forces_one_continuation(workdir):
    agent, mock = make_agent(["first answer", "second answer"], workdir)
    state = {"calls": 0}

    def stop_once(messages):
        state["calls"] += 1
        return "keep going" if state["calls"] == 1 else None

    agent.hooks.register("Stop", stop_once)
    result = agent.run("do something")
    assert result.text == "second answer"
    assert len(mock.calls) == 2
    # 强制续跑的消息被注入为最后一条 user 消息
    assert mock.calls[1]["messages"][-1]["content"] == "keep going"


def test_user_prompt_submit_can_replace_input(workdir):
    agent, mock = make_agent(["ok"], workdir)
    agent.hooks.register("UserPromptSubmit", lambda q: f"{q} (cwd injected)")
    agent.run("list files")
    assert mock.calls[0]["messages"][0]["content"] == "list files (cwd injected)"


def test_post_tool_use_observes_output(workdir):
    seen = []
    agent, _ = make_agent(
        [[("bash", {"command": "echo observable"})], "done"], workdir
    )
    agent.hooks.register("PostToolUse", lambda block, out: seen.append(out) or None)
    agent.run("echo something")
    assert seen and "observable" in seen[0]


def test_unknown_event_rejected(workdir):
    from agentloop.hooks import HookRegistry

    registry = HookRegistry()
    try:
        registry.register("NotAnEvent", lambda: None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
