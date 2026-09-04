"""命令行入口 —— python -m agentloop [prompt] 或安装后 agentloop 命令。

不带参数进入 REPL（会话消息跨轮保留，压缩管线负责控制增长），
带参数则单次执行。工具调用以暗色日志打印，退出时输出 token 统计。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from .agent import Agent
from .compact import Compactor
from .hooks import HookRegistry
from .models import FallbackClient, build_client
from .permission import AskUser, PermissionGate
from .tools import build_toolbox


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 加载：只补缺、不覆盖已有环境变量。"""
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _dim(message: str) -> None:
    print(f"\033[90m{message}\033[0m")


def _short(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 100 else text[:100] + "…"


def build_default_agent(
    workdir: Path,
    verbose_tools: bool = True,
    ask_user: AskUser | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Agent:
    client = build_client(should_stop=should_stop)

    # 备用模型：AGENTLOOP_FALLBACK_MODELS="a,b"（可跨提供商）
    fallbacks = [
        m.strip()
        for m in os.environ.get("AGENTLOOP_FALLBACK_MODELS", "").split(",")
        if m.strip()
    ]
    if fallbacks:
        client = FallbackClient(
            [client, *(_build_named(m, should_stop) for m in fallbacks)]
        )

    toolbox, _todo = build_toolbox(workdir, should_stop=should_stop)
    hooks = HookRegistry()
    hooks.register("PreToolUse", PermissionGate(ask_user=ask_user).as_hook())
    if verbose_tools:
        hooks.register(
            "PreToolUse",
            lambda b: _dim(f"→ {b['name']}({_short(b.get('input', {}))})") or None,
        )
        hooks.register(
            "PostToolUse",
            lambda b, out: (
                _dim(
                    f"← {b['name']}: "
                    f"{str(out).splitlines()[0][:90] if out else '(empty)'}"
                )
                or None
            ),
        )
    compactor = Compactor(workdir, client=client)

    system_prompt = (
        f"You are AgentLoop, a coding agent working in {workdir}. "
        "Use tools to solve tasks; act, don't explain. "
        "For multi-step tasks, call todo_write first and keep it updated. "
        "After running verification commands, state the command and its exit code "
        "explicitly in your reply."
    )
    return Agent(
        client,
        toolbox,
        hooks,
        compactor,
        system_prompt,
        should_stop=should_stop,
    )


def _build_named(model: str, should_stop: Callable[[], bool] | None = None):
    """为备用模型临时切换 AGENTLOOP_MODEL 再构建。"""
    old = os.environ.get("AGENTLOOP_MODEL")
    os.environ["AGENTLOOP_MODEL"] = model
    try:
        return build_client(model, should_stop=should_stop)
    finally:
        if old is None:
            os.environ.pop("AGENTLOOP_MODEL", None)
        else:
            os.environ["AGENTLOOP_MODEL"] = old


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "web":
        from .web import main as web_main

        return web_main(argv[1:])

    _load_dotenv()
    workdir = Path.cwd()

    try:
        agent = build_default_agent(workdir)
    except SystemExit as exc:
        print(exc)
        return 2

    print(f"AgentLoop 0.1.0 — model: {agent.client.model}  workdir: {workdir}")
    print("   输入任务开始；q / exit 退出。\n")

    total_usage = {"input_tokens": 0, "output_tokens": 0}

    def _report(result) -> None:
        for key in total_usage:
            total_usage[key] += result.usage.get(key, 0)

    if argv:  # 单次模式
        try:
            result = agent.run(" ".join(argv))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ {type(exc).__name__}: {exc}")
            return 1
        _report(result)
        print(result.text)
        input_tokens = total_usage["input_tokens"]
        output_tokens = total_usage["output_tokens"]
        _dim(
            f"\n[usage] turns={result.turns}  input_tokens={input_tokens}  "
            f"output_tokens={output_tokens}"
        )
        return 0

    session: list = []
    while True:
        try:
            line = input("agentloop >> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in {"q", "quit", "exit"}:
            break
        try:
            result = agent.run(line, session)
        except KeyboardInterrupt:
            _dim("\n[interrupted]")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ {type(exc).__name__}: {exc}")
            continue
        session = result.messages
        _report(result)
        print(f"\n{result.text}\n")

    _dim(
        f"[usage] input_tokens={total_usage['input_tokens']}  "
        f"output_tokens={total_usage['output_tokens']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
