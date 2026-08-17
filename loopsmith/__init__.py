"""LoopSmith — 手工锻造的最小可用 Coding Agent Harness。

核心理念（学自 learn-claude-code / Claude Code）：
Agency 来自模型，Harness 是载具。循环属于 agent，机制属于 harness。
所有机制（权限、hooks、压缩、路由）都挂在循环的边界上，循环本身保持 30 行不变。

模块地图：
    agent.py     内核循环（为什么它只有 30 行）
    tools.py     工具注册与查表分发
    permission.py 三道权限闸门，以 PreToolUse hook 挂载
    hooks.py     四个扩展插口
    compact.py   四步上下文压缩管线
    models.py    多提供商模型路由（OpenAI 兼容 / Anthropic / Mock / Fallback）
    cli.py       REPL 入口
"""

__version__ = "0.1.0"
