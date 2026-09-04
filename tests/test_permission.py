from helpers import make_agent, tool_results


def test_deny_list_hard_blocks(workdir):
    agent, _ = make_agent([[("bash", {"command": "rm -rf /"})], "done"], workdir)
    result = agent.run("clean the machine")
    assert any("deny list" in r for r in tool_results(result.messages))


def test_risky_command_denied_by_user(workdir):
    agent, _ = make_agent(
        [[("bash", {"command": "rm notes.txt"})], "done"],
        workdir,
        ask=lambda *a: False,
    )
    result = agent.run("delete notes")
    assert any("denied by user" in r for r in tool_results(result.messages))


def test_risky_command_allowed_by_user(workdir):
    (workdir / "notes.txt").write_text("delete me")
    agent, _ = make_agent(
        [[("bash", {"command": "rm notes.txt"})], "done"],
        workdir,
        ask=lambda *a: True,
    )
    result = agent.run("delete notes")
    assert any("exit=0" in r for r in tool_results(result.messages))


def test_normal_command_never_asks(workdir):
    asked = []

    def ask(tool, args, reason):
        asked.append((tool, args))
        return True

    agent, _ = make_agent(
        [[("bash", {"command": "echo hi"})], "done"], workdir, ask=ask
    )
    agent.run("say hi")
    assert asked == []  # 普通命令三道闸门全放行，不进入审批


def test_denial_reaches_model_not_crash(workdir):
    """被拒绝的调用必须返回 tool_result，循环继续，模型最终仍给出回答。"""
    agent, mock = make_agent(
        [[("bash", {"command": "sudo rm x"})], "ok, I will try another way"],
        workdir,
    )
    result = agent.run("try sudo")
    assert result.text == "ok, I will try another way"
    assert len(mock.calls) == 2  # 循环没有断
