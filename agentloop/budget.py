"""请求上下文预算。

这里的预算器不假装知道任一模型的 tokenizer。默认估算器用 UTF-8 字节数做
保守近似，调用方可以注入与实际模型匹配的计数函数。所有会发送给模型的
system、tools 和 messages 都必须经由同一份 JSON payload 计算。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class ContextBudgetError(RuntimeError):
    """本地预算不足，和提供商返回的上下文超限错误区分开。"""


TokenEstimator = Callable[[str], int]


def estimate_utf8_tokens(text: str) -> int:
    """按每个 UTF-8 字节一个估算 token 的保守近似估算。

    这不是任何模型的精确 tokenizer；它刻意偏保守，并允许用
    ``RequestBudget(token_estimator=...)`` 替换。
    """

    return len(text.encode("utf-8"))


class RequestBudget:
    """计算一次模型请求的输入预算。

    ``context_window_tokens`` 是调用方按模型配置给出的窗口大小。输出预留和
    安全边际从窗口中扣除，剩余部分才可用于请求输入。
    """

    _REQUEST_ENVELOPE_TOKENS = 12
    _SYSTEM_ENVELOPE_TOKENS = 4
    _MESSAGE_ENVELOPE_TOKENS = 4
    _TOOL_ENVELOPE_TOKENS = 8

    def __init__(
        self,
        context_window_tokens: int,
        reserve_output_tokens: int,
        safety_margin_tokens: int = 0,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self.context_window_tokens = self._positive_int(
            "context_window_tokens", context_window_tokens
        )
        self.reserve_output_tokens = self._non_negative_int(
            "reserve_output_tokens", reserve_output_tokens
        )
        self.safety_margin_tokens = self._non_negative_int(
            "safety_margin_tokens", safety_margin_tokens
        )
        if (
            self.reserve_output_tokens + self.safety_margin_tokens
            >= self.context_window_tokens
        ):
            raise ValueError(
                "reserve_output_tokens + safety_margin_tokens must be smaller "
                "than context_window_tokens"
            )
        if token_estimator is not None and not callable(token_estimator):
            raise TypeError("token_estimator must be callable")
        self.token_estimator = token_estimator or estimate_utf8_tokens

    @staticmethod
    def _positive_int(name: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _non_negative_int(name: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @property
    def input_limit_tokens(self) -> int:
        """窗口中可用于输入的 token 数。"""

        return (
            self.context_window_tokens
            - self.reserve_output_tokens
            - self.safety_margin_tokens
        )

    def estimate(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: Any = None,
    ) -> int:
        """估算完整请求输入，包括 JSON 和每种协议项的包装开销。"""

        payload = {"system": system, "tools": tools or [], "messages": messages}
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        content_tokens = self._count(serialized)
        envelope_tokens = self._REQUEST_ENVELOPE_TOKENS
        if system:
            envelope_tokens += self._SYSTEM_ENVELOPE_TOKENS
        envelope_tokens += len(messages) * self._MESSAGE_ENVELOPE_TOKENS
        envelope_tokens += self._tool_count(tools) * self._TOOL_ENVELOPE_TOKENS
        return content_tokens + envelope_tokens

    def available_input_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: Any = None,
    ) -> int:
        """在输出预留与安全边际后，输入还剩多少 token；可为负数。"""

        return self.input_limit_tokens - self.estimate(messages, system, tools)

    def fits(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: Any = None,
    ) -> bool:
        """当前请求是否可在配置的上下文窗口中发送。"""

        return self.available_input_tokens(messages, system, tools) >= 0

    def _count(self, text: str) -> int:
        count = self.token_estimator(text)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("token_estimator must return a non-negative integer")
        return count

    @staticmethod
    def _tool_count(tools: Any) -> int:
        if tools is None:
            return 0
        if isinstance(tools, (list, tuple)):
            return len(tools)
        return 1
