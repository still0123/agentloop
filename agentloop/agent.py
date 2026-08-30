"""Agent Loop 内核（对应课程 s01——17 章过去，这里始终 30 行左右）。

循环的退出条件只有一个信号：模型回复里不再有 tool_use 块。
其余一切都是"挂"上来的：
    轮首：  compactor.prepare()     —— 压缩管线（s08）
    输入前：UserPromptSubmit hooks  —— 注入/改写输入（s04）
    执行前：PreToolUse hooks        —— 权限闸门挂在这里（s03→s04）
    执行后：PostToolUse hooks       —— 日志/审计（s04）
    退出前：Stop hooks              —— 收尾或强制续跑（s04/s17 的雏形）
    工具层：查表分发                 —— 加工具不改循环（s02）
    计划：  todo reminder 注入       —— 3 轮不更新计划就提醒（s05）
    恢复：  prompt_too_long → reactive compact，重试一次（s08）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hooks import HookRegistry
from .tools import Toolbox


@dataclass
class RunResult:
    text: str
    messages: list
    turns: int
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    stopped_reason: str = "done"  # done | max_turns


class Agent:
    def __init__(
        self,
        client,
        toolbox: Toolbox,
        hooks: HookRegistry,
        compactor,
        system_prompt: str,
        max_turns: int = 40,
        reactive_retries: int = 1,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.hooks = hooks
        self.compactor = compactor
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.reactive_retries = reactive_retries

    def run(self, user_input: str, messages: list | None = None) -> RunResult:
        # UserPromptSubmit hook 可返回 str 替换输入（上下文注入的口子）
        replaced = self.hooks.trigger("UserPromptSubmit", user_input)
        if isinstance(replaced, str) and replaced:
            user_input = replaced

        messages = list(messages) if messages else []
        messages.append({"role": "user", "content": user_input})

        usage = {"input_tokens": 0, "output_tokens": 0}
        turns = 0
        reactive_retries = 0
        todo_gap = 0  # 连续多少轮工具调用没碰过 todo_write

        while True:
            messages = self.compactor.prepare(messages)

            try:
                response = self.client.complete(
                    self.system_prompt, messages, self.toolbox.defs
                )
            except Exception as exc:  # 估算失误导致超限 → 补救一次
                if (
                    reactive_retries < self.reactive_retries
                    and _is_prompt_too_long(exc)
                ):
                    messages = self.compactor.reactive_compact(messages)
                    reactive_retries += 1
                    continue
                raise

            for key in usage:
                usage[key] += response.usage.get(key, 0)
            turns += 1
            messages.append({"role": "assistant", "content": response.blocks})

            tool_calls = [
                b for b in response.blocks if b.get("type") == "tool_use"
            ]
            if not tool_calls:
                # 模型想停 → Stop hook 有最后一次否决权（返回 str 强制续跑）
                force = self.hooks.trigger("Stop", messages)
                if isinstance(force, str) and force:
                    messages.append({"role": "user", "content": force})
                    continue
                return RunResult(
                    text=response.text or "(no text)",
                    messages=messages,
                    turns=turns,
                    usage=usage,
                )

            if turns >= self.max_turns:
                return RunResult(
                    text=f"(stopped: reached max_turns={self.max_turns})",
                    messages=messages,
                    turns=turns,
                    usage=usage,
                    stopped_reason="max_turns",
                )

            results = []
            used_todo = any(b.get("name") == "todo_write" for b in tool_calls)
            for block in tool_calls:
                blocked = self.hooks.trigger("PreToolUse", block)
                if blocked is not None:
                    # 拒绝原因作为 tool_result 返回——模型看得到，可以改道
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": str(blocked),
                    })
                    continue
                output = self.toolbox.execute(block)
                self.hooks.trigger("PostToolUse", block, output)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": output,
                })

            # s05 reminder：连续 3 轮没更新计划，把提醒拍在结果后面
            todo_gap = 0 if used_todo else todo_gap + 1
            if todo_gap >= 3:
                results.append({
                    "type": "text",
                    "text": "<reminder>Update your todos.</reminder>",
                })
                todo_gap = 0

            messages.append({"role": "user", "content": results})


def _is_prompt_too_long(exc: Exception) -> bool:
    text = str(exc).lower()
    return "prompt_too_long" in text or "too many tokens" in text or "context length" in text
