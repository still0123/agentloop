"""测试工厂：组装一个用 MockClient 驱动的完整 Agent，不碰网络。"""

from loopsmith.agent import Agent
from loopsmith.compact import Compactor
from loopsmith.hooks import HookRegistry
from loopsmith.models import MockClient
from loopsmith.permission import PermissionGate, allow_all
from loopsmith.tools import build_toolbox


def make_agent(
    turns,
    workdir,
    ask=None,
    compactor_kwargs=None,
    max_turns=40,
):
    """返回 (agent, mock_client)。

    ask:          权限闸门 3 的应答回调（默认全自动放行）
    compactor_kwargs: 覆盖压缩参数（如 batch_budget / char_limit）；
                  默认 char_limit 极大，摘要步不会触发
    """
    mock = MockClient(turns)
    toolbox, _todo = build_toolbox(workdir)
    hooks = HookRegistry()
    hooks.register("PreToolUse", PermissionGate(ask_user=ask or allow_all).as_hook())

    kwargs = {"char_limit": 10 ** 12}
    if compactor_kwargs:
        kwargs.update(compactor_kwargs)
    # 摘要客户端独立于主 mock，避免吃掉对话脚本
    compactor = Compactor(workdir, client=MockClient(["(summary)"]), **kwargs)

    agent = Agent(mock, toolbox, hooks, compactor, system_prompt="test", max_turns=max_turns)
    return agent, mock


def tool_results(messages):
    """收集全部 tool_result 内容，便于断言。"""
    out = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    out.append(block["content"])
    return out
