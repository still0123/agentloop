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

from collections.abc import Callable
from dataclasses import dataclass, field

from .hooks import HookRegistry
from .tools import Toolbox

EventCallback = Callable[[dict], None]
StopCheck = Callable[[], bool]


@dataclass
class RunResult:
    text: str
    messages: list
    turns: int
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    stopped_reason: str = "done"  # done | max_turns | cancelled


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
        should_stop: StopCheck | None = None,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.hooks = hooks
        self.compactor = compactor
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.reactive_retries = reactive_retries
        self.should_stop = should_stop or (lambda: False)

    def run(
        self,
        user_input: str,
        messages: list | None = None,
        on_event: EventCallback | None = None,
    ) -> RunResult:
        emit = on_event or (lambda event: None)
        try:
            return self._run(user_input, messages, emit)
        except Exception as exc:
            try:
                emit(
                    {
                        "type": "error",
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            except Exception:
                pass
            raise

    def _run(
        self, user_input: str, messages: list | None, emit: EventCallback
    ) -> RunResult:
        # UserPromptSubmit hook 可返回 str 替换输入（上下文注入的口子）
        replaced = self.hooks.trigger("UserPromptSubmit", user_input)
        if isinstance(replaced, str) and replaced:
            user_input = replaced

        messages = list(messages) if messages else []
        messages.append({"role": "user", "content": user_input})
        emit({"type": "run_start", "prompt": user_input})

        usage = {"input_tokens": 0, "output_tokens": 0}
        turns = 0
        reactive_retries = 0
        todo_gap = 0  # 连续多少轮工具调用没碰过 todo_write

        while True:
            if self.should_stop():
                return _cancelled_result(messages, turns, usage, emit)
            messages = self.compactor.prepare(messages)
            emit({"type": "model_start", "turn": turns + 1})

            try:
                response = self.client.complete(
                    self.system_prompt, messages, self.toolbox.defs
                )
            except Exception as exc:  # 估算失误导致超限 → 补救一次
                if reactive_retries < self.reactive_retries and _is_prompt_too_long(
                    exc
                ):
                    messages = self.compactor.reactive_compact(messages)
                    reactive_retries += 1
                    continue
                raise

            for key in usage:
                usage[key] += response.usage.get(key, 0)
            turns += 1
            if self.should_stop():
                return _cancelled_result(messages, turns, usage, emit)
            messages.append({"role": "assistant", "content": response.blocks})
            if response.text:
                emit(
                    {
                        "type": "assistant_message",
                        "text": response.text,
                        "turn": turns,
                    }
                )

            tool_calls = [b for b in response.blocks if b.get("type") == "tool_use"]
            if not tool_calls:
                # 模型想停 → Stop hook 有最后一次否决权（返回 str 强制续跑）
                force = self.hooks.trigger("Stop", messages)
                if isinstance(force, str) and force:
                    messages.append({"role": "user", "content": force})
                    continue
                result = RunResult(
                    text=response.text or "(no text)",
                    messages=messages,
                    turns=turns,
                    usage=usage,
                )
                emit(
                    {
                        "type": "done",
                        "text": result.text,
                        "turns": result.turns,
                        "usage": dict(result.usage),
                        "stopped_reason": result.stopped_reason,
                    }
                )
                return result

            if turns >= self.max_turns:
                result = RunResult(
                    text=f"(stopped: reached max_turns={self.max_turns})",
                    messages=messages,
                    turns=turns,
                    usage=usage,
                    stopped_reason="max_turns",
                )
                emit(
                    {
                        "type": "done",
                        "text": result.text,
                        "turns": result.turns,
                        "usage": dict(result.usage),
                        "stopped_reason": result.stopped_reason,
                    }
                )
                return result

            results = []
            used_todo = any(b.get("name") == "todo_write" for b in tool_calls)
            for index, block in enumerate(tool_calls):
                if self.should_stop():
                    _append_cancelled_tools(messages, results, tool_calls[index:], emit)
                    return _cancelled_result(messages, turns, usage, emit)
                emit(
                    {
                        "type": "tool_call",
                        "id": block["id"],
                        "name": block["name"],
                        "input": dict(block.get("input", {})),
                    }
                )
                blocked = self.hooks.trigger("PreToolUse", block)
                if self.should_stop():
                    _append_cancelled_tools(messages, results, tool_calls[index:], emit)
                    return _cancelled_result(messages, turns, usage, emit)
                if blocked is not None:
                    # 拒绝原因作为 tool_result 返回——模型看得到，可以改道
                    result_block = {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": str(blocked),
                    }
                    results.append(result_block)
                    emit(
                        {
                            "type": "tool_result",
                            "id": block["id"],
                            "name": block["name"],
                            "content": result_block["content"],
                            "blocked": True,
                        }
                    )
                    continue
                output = self.toolbox.execute(block)
                self.hooks.trigger("PostToolUse", block, output)
                cancelled = self.should_stop()
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": output,
                    }
                )
                emit(
                    {
                        "type": "tool_result",
                        "id": block["id"],
                        "name": block["name"],
                        "content": output,
                        "blocked": False,
                        "cancelled": cancelled,
                    }
                )
                if cancelled:
                    _append_cancelled_tools(
                        messages, results, tool_calls[index + 1 :], emit
                    )
                    return _cancelled_result(messages, turns, usage, emit)

            # s05 reminder：连续 3 轮没更新计划，把提醒拍在结果后面
            todo_gap = 0 if used_todo else todo_gap + 1
            if todo_gap >= 3:
                results.append(
                    {
                        "type": "text",
                        "text": "<reminder>Update your todos.</reminder>",
                    }
                )
                todo_gap = 0

            messages.append({"role": "user", "content": results})


def _append_cancelled_tools(
    messages: list, results: list, pending: list, emit: EventCallback
) -> None:
    for block in pending:
        content = "Error: cancelled by user"
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content,
            }
        )
        emit(
            {
                "type": "tool_result",
                "id": block["id"],
                "name": block["name"],
                "content": content,
                "blocked": False,
                "cancelled": True,
            }
        )
    if results:
        messages.append({"role": "user", "content": results})


def _cancelled_result(
    messages: list, turns: int, usage: dict, emit: EventCallback
) -> RunResult:
    text = "(cancelled by user)"
    messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    result = RunResult(
        text=text,
        messages=messages,
        turns=turns,
        usage=usage,
        stopped_reason="cancelled",
    )
    emit(
        {
            "type": "done",
            "text": result.text,
            "turns": result.turns,
            "usage": dict(result.usage),
            "stopped_reason": result.stopped_reason,
        }
    )
    return result


def _is_prompt_too_long(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "prompt_too_long" in text
        or "too many tokens" in text
        or "context length" in text
    )
