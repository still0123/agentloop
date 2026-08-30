"""Hook 系统 —— 扩展点不侵入循环（对应课程 s04）。

为什么需要 hooks：
    如果把权限检查、日志、审计直接写进 agent_loop，每加一个功能都要改循环，
    循环很快膨胀到没人敢动。正确做法是循环只留"插口"，功能做成"插头"。

设计约束：
    trigger 遇到第一个返回非 None 的回调就停止（短路）。
    - PreToolUse 返回 str  → 拦截本次工具执行，str 作为拒绝原因
    - Stop 返回 str        → 阻止退出，str 注入为新消息强制续跑
    - UserPromptSubmit 返回 str → 替换用户输入（上下文注入）
    - PostToolUse 的返回值不参与控制流（纯观察）
"""

from __future__ import annotations

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list] = {event: [] for event in EVENTS}

    def register(self, event: str, callback) -> None:
        if event not in self._hooks:
            raise ValueError(f"unknown hook event: {event!r}, expected one of {EVENTS}")
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        """依次执行回调；第一个返回非 None 的结果直接作为控制信号返回。"""
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None
