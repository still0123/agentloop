"""工具层 —— 注册与查表分发（对应课程 s02）+ TodoWrite 计划工具（s05）。

设计要点：
    加一个工具 = add() 一次（描述 + schema + handler），循环一行不改。
    execute() 把一切异常都转成 "Error: ..." 字符串返回给模型——
    模型看到错误可以下一轮自己修正参数，脚本永不因工具 bug 崩溃。
    文件工具经 safe_path 限制在 workdir 内；bash 不受限（由权限闸门管，
    这是刻意的职责分离：路径越界是确定性错误，命令风险是策略问题）。
"""

from __future__ import annotations

import glob as globlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_TOOL_OUTPUT = 200_000  # 超长输出截断；更大的交给压缩管线的转存机制


def safe_path(workdir: Path, path: str) -> Path:
    """把用户/模型给的路径钉在工作区内，越界直接抛错（会被 execute 转成 Error）。"""
    workdir = workdir.resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else workdir / candidate).resolve()
    if not resolved.is_relative_to(workdir):
        raise ValueError(f"path escapes workspace: {path}")
    return resolved


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable


class Toolbox:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def add(
        self, name: str, description: str, input_schema: dict, handler: Callable
    ) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        self._tools[name] = ToolDef(name, description, input_schema, handler)

    @property
    def defs(self) -> list[dict]:
        """给模型看的工具定义（每轮组装进请求）。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, block: dict) -> str:
        tool = self._tools.get(block.get("name"))
        if tool is None:
            return f"Error: unknown tool '{block.get('name')}'"
        try:
            return str(tool.handler(**block.get("input", {})))
        except TypeError as exc:
            return f"Error: bad arguments for {tool.name}: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 工具错误必须回到模型，而不是炸掉循环
            return f"Error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# TodoManager（s05）：规划能力，不是执行能力
# ---------------------------------------------------------------------------


class TodoManager:
    """内存中的计划清单。约束：≤20 项、内容非空、同时最多一个 in_progress。"""

    MAX_ITEMS = 20
    STATUSES = ("pending", "in_progress", "completed")
    _MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

    def __init__(self) -> None:
        self.items: list[dict] = []

    def update(self, todos) -> str:
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        if len(todos) > self.MAX_ITEMS:
            raise ValueError(f"too many todo items (max {self.MAX_ITEMS})")
        validated = []
        for item in todos:
            if not isinstance(item, dict):
                raise ValueError("each todo item must be an object")
            content = str(item.get("content", "")).strip()
            status = item.get("status", "pending")
            if not content:
                raise ValueError("todo item content must be non-empty")
            if status not in self.STATUSES:
                raise ValueError(
                    f"invalid status {status!r}, expected one of {self.STATUSES}"
                )
            validated.append({"content": content, "status": status})
        if sum(1 for i in validated if i["status"] == "in_progress") > 1:
            raise ValueError("only one todo item can be in_progress at a time")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "(todo list is empty)"
        return "\n".join(
            f"{self._MARKS[i['status']]} {i['content']}" for i in self.items
        )


# ---------------------------------------------------------------------------
# 工具箱组装
# ---------------------------------------------------------------------------


def build_toolbox(workdir: Path, should_stop: Callable[[], bool] | None = None):
    """返回 (Toolbox, TodoManager)。workdir 由调用方钉死，工具闭包引用它。"""
    workdir = Path(workdir)
    should_stop = should_stop or (lambda: False)
    todo = TodoManager()
    box = Toolbox()

    def run_bash(command: str, timeout: int = 60) -> str:
        if should_stop():
            return "Error: command cancelled by user"
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workdir),
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if should_stop():
                _terminate_process(proc)
                return "Error: command cancelled by user"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(proc)
                return f"Error: command timed out after {timeout}s"
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        parts = [f"exit={proc.returncode}"]
        if stdout:
            parts.append("[stdout]\n" + stdout)
        if stderr:
            parts.append("[stderr]\n" + stderr)
        text = "\n".join(parts)
        if len(text) > MAX_TOOL_OUTPUT:
            text = (
                text[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(text)} chars total)"
            )
        return text

    def run_read(path: str, limit: int | None = None) -> str:
        lines = safe_path(workdir, path).read_text(encoding="utf-8").splitlines()
        if limit is not None:
            lines = lines[:limit]
        return "\n".join(lines) if lines else "(empty file)"

    def run_write(path: str, content: str) -> str:
        target = safe_path(workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"

    def run_edit(path: str, old_text: str, new_text: str) -> str:
        target = safe_path(workdir, path)
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0:
            return f"Error: old_text not found in {path}"
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        note = f" (first of {occurrences} occurrences)" if occurrences > 1 else ""
        return f"Edited {path}{note}"

    def run_glob(pattern: str) -> str:
        matches = sorted(globlib.glob(pattern, root_dir=str(workdir), recursive=True))
        if not matches:
            return "(no matches)"
        if len(matches) > 500:
            matches = matches[:500] + [f"... ({len(matches)} matches total)"]
        return "\n".join(matches)

    def run_todo(todos) -> str:
        output = todo.update(todos)
        return output

    box.add(
        "bash",
        "Run a shell command in the workspace and return exit code and output.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
        run_bash,
    )
    box.add(
        "read_file",
        "Read a text file inside the workspace.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
        run_read,
    )
    box.add(
        "write_file",
        "Create or overwrite a text file inside the workspace.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        run_write,
    )
    box.add(
        "edit_file",
        "Replace the first occurrence of old_text with new_text in a file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
        run_edit,
    )
    box.add(
        "glob",
        "Find files by glob pattern, e.g. '**/*.py'.",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        run_glob,
    )
    box.add(
        "todo_write",
        "Create or replace the session todo list. "
        "Plan before executing multi-step tasks.",
        {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                    },
                }
            },
            "required": ["todos"],
        },
        run_todo,
    )
    return box, todo


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
