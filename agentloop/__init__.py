"""AgentLoop — 可扩展的通用 AI Agent。

Agent 负责模型驱动的决策与 ReAct 循环；Harness 在循环边界提供权限、
钩子、上下文管理和模型路由。

模块地图：
    agent.py     ReAct 执行循环
    tools.py     工具注册与查表分发
    permission.py 三道权限闸门，以 PreToolUse hook 挂载
    hooks.py     四个扩展插口
    compact.py   上下文预算、归档与压缩
    models.py    多提供商模型路由（OpenAI 兼容 / Anthropic / Mock / Fallback）
    cli.py       REPL 入口
"""

__version__ = "0.1.1"
