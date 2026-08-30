"""权限闸门（对应课程 s03），以 PreToolUse hook 的形式挂载（s04 的教训）。

三道闸门，按"拦截成本从低到高"排列：
    1. 拒绝列表  永远禁止的模式，字符串匹配，零成本硬拒绝
    2. 规则匹配  有风险但视情况的操作（rm / chmod 777 ...）
    3. 用户审批  暂停等用户拍板（ask_user 回调可注入，测试时自动应答）

为什么拒绝列表只是字符串匹配也够用（教学版）：
    它说明的是"闸门的位置在工具执行之前"这个结构性事实。
    生产级实现会换成沙箱 / 系统调用过滤 / 提示注入检测，
    但"先判后执行"的管线形状不变。
"""

from __future__ import annotations

from typing import Callable

# 闸门 1：无条件禁止。宁可误伤，不可放过。
DENY_PATTERNS = (
    "rm -rf /",
    "sudo ",
    "mkfs",
    "dd if=",
    "> /dev/sd",
    "shutdown",
    "reboot",
)

# 闸门 2：有风险的关键词，命中后交给用户决定。
RISKY_BASH_KEYWORDS = ("rm ", "chmod 777", "> /etc/", "| sh", "| bash")

AskUser = Callable[[str, dict, str], bool]


class PermissionGate:
    """返回 None = 放行；返回 str = 拒绝原因（会成为 tool_result，模型可据此改道）。"""

    def __init__(
        self,
        ask_user: AskUser | None = None,
        deny_patterns: tuple = DENY_PATTERNS,
        risky_keywords: tuple = RISKY_BASH_KEYWORDS,
    ) -> None:
        self._ask = ask_user or _interactive_ask
        self.deny_patterns = deny_patterns
        self.risky_keywords = risky_keywords

    def as_hook(self):
        """包装成 PreToolUse hook —— 循环从此不认识"权限"，只认识 hooks。"""
        return self.check

    def check(self, block) -> str | None:
        if block.get("name") == "bash":
            command = str(block.get("input", {}).get("command", ""))
            for pattern in self.deny_patterns:  # 闸门 1
                if pattern in command:
                    return f"Permission denied: '{pattern}' matches the deny list."
            if any(kw in command for kw in self.risky_keywords):  # 闸门 2 → 3
                reason = "potentially destructive command"
                if not self._ask(block["name"], block.get("input", {}), reason):
                    return "Permission denied by user."
        return None


def _interactive_ask(tool_name: str, args: dict, reason: str) -> bool:
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return choice in ("y", "yes")


def allow_all(tool_name: str, args: dict, reason: str) -> bool:
    """测试 / 演示用的自动放行。"""
    return True


def deny_all(tool_name: str, args: dict, reason: str) -> bool:
    return False
