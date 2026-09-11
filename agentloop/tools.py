"""工具注册、分发与待办管理。

设计要点：
    加一个工具 = add() 一次（描述 + schema + handler），循环一行不改。
    execute() 把一切异常都转成 "Error: ..." 字符串返回给模型——
    模型看到错误可以下一轮自己修正参数，脚本永不因工具 bug 崩溃。
    文件工具经 safe_path 限制在 workdir 内；bash 不受限（由权限闸门管，
    这是刻意的职责分离：路径越界是确定性错误，命令风险是策略问题）。
"""

from __future__ import annotations

import fnmatch
import os
from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .artifacts import ToolOutput, read_artifact

MAX_READ_LINES = 200
MAX_READ_CHARS = 50_000
MAX_SEARCH_MATCHES = 50
MAX_SEARCH_CONTEXT = 10
MAX_SEARCH_CHARS = 50_000
MAX_SOURCE_LINE_CHARS = 16_000
MAX_SEARCH_SOURCE_BYTES = 10_000_000
MAX_SEARCH_QUERY_CHARS = 1_024
SEARCH_CHUNK_CHARS = 8_192
MATCH_EXCERPT_CHARS = 200
CONTEXT_LINE_CHARS = 4_000


def _bounded_lines(path: Path):
    """Yield text lines without ever retaining an arbitrarily long source line.

    The third result is true when the source line was longer than the retained
    prefix.  Draining that line still lets the caller continue at the next line.
    """
    with path.open(encoding="utf-8") as handle:
        line_number = 0
        while fragment := handle.readline(MAX_SOURCE_LINE_CHARS + 1):
            line_number += 1
            too_long = (
                not fragment.endswith("\n") and len(fragment) > MAX_SOURCE_LINE_CHARS
            )
            if too_long:
                prefix = fragment
                # Do not join the rest of a huge line into memory.
                while fragment and not fragment.endswith("\n"):
                    fragment = handle.readline(MAX_SOURCE_LINE_CHARS + 1)
                text = prefix.rstrip("\r\n")
            else:
                text = fragment.rstrip("\r\n")
            yield line_number, text, too_long


def _positive_int(value: int | None, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def safe_path(workdir: Path, path: str) -> Path:
    """把用户/模型给的路径钉在工作区内，越界直接抛错（会被 execute 转成 Error）。"""
    workdir = workdir.resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else workdir / candidate).resolve()
    if not resolved.is_relative_to(workdir):
        raise ValueError(f"path escapes workspace: {path}")
    return resolved


def _glob_matches(path: str, pattern: str) -> bool:
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return (
            i < len(parts)
            and fnmatch.fnmatchcase(parts[i], patterns[j])
            and match(i + 1, j + 1)
        )

    return match(0, 0)


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
            output = tool.handler(**block.get("input", {}))
            return output if isinstance(output, ToolOutput) else str(output)
        except TypeError as exc:
            return f"Error: bad arguments for {tool.name}: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 工具错误必须回到模型，而不是炸掉循环
            return f"Error: {type(exc).__name__}: {exc}"

    def select(self, names: Collection[str]) -> Toolbox:
        """Reuse handlers without giving an embedding application unwanted tools."""
        unknown = set(names) - set(self.names)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        selected = Toolbox()
        selected._tools = {
            name: tool for name, tool in self._tools.items() if name in names
        }
        return selected

    def extend(self, other: Toolbox) -> None:
        """Compose capability sets atomically; never silently replace a handler."""
        duplicates = set(self.names) & set(other.names)
        if duplicates:
            raise ValueError(f"duplicate tool names: {sorted(duplicates)}")
        self._tools.update(other._tools)


# ---------------------------------------------------------------------------
# TodoManager：用于维护计划状态，不执行计划项。
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


def build_toolbox(
    workdir: Path,
    should_stop: Callable[[], bool] | None = None,
    *,
    include: Collection[str] | None = None,
    read_roots: Mapping[str, Path] | None = None,
):
    """返回 (Toolbox, TodoManager)。workdir 由调用方钉死，工具闭包引用它。"""
    workdir = Path(workdir).resolve()
    roots = {"workspace": workdir}
    for name, path in (read_roots or {}).items():
        if not name or name == "workspace":
            raise ValueError("read root names must be nonempty and not 'workspace'")
        root_path = Path(path).resolve()
        if not root_path.is_dir():
            raise ValueError(f"read root is not a directory: {name}")
        roots[name] = root_path

    def read_root(name):
        if name not in roots:
            raise ValueError(f"unknown read root: {name}; available: {list(roots)}")
        return roots[name]

    should_stop = should_stop or (lambda: False)
    todo = TodoManager()
    box = Toolbox()

    def run_bash(command: str, timeout: int = 60) -> str:
        from .command import run_command

        return run_command(workdir, command, timeout, should_stop)

    def run_read(
        path: str,
        limit: int | None = None,
        offset: int | None = None,
        root: str = "workspace",
        char_offset: int = 0,
        max_chars: int = 6000,
    ) -> str:
        """Read a bounded page.  Offset is 1-based to match editor line numbers."""
        target = safe_path(read_root(root), path)
        if target.is_relative_to(
            workdir.resolve() / ".task_outputs/tool-results"
        ) or target.is_relative_to(workdir.resolve() / ".transcripts"):
            if offset is not None or limit is not None:
                raise ValueError(
                    "Archives use char_offset/max_chars; follow next_char_offset"
                )
            return read_artifact(workdir, target, char_offset, max_chars)
        requested_limit = limit
        paged = offset is not None
        if offset is None:
            offset = 1
        offset = _positive_int(offset, "offset", 1_000_000)
        if limit is None:
            limit = MAX_READ_LINES
        else:
            limit = _positive_int(limit, "limit", MAX_READ_LINES)

        lines: list[str] = []
        returned = 0
        chars = 0
        next_line: int | None = None
        long_line = False
        for line_number, text, source_was_long in _bounded_lines(target):
            if line_number < offset:
                continue
            rendered = text
            if source_was_long:
                rendered += "\n... (source line truncated)"
                long_line = True
            needed = len(rendered) + (1 if lines else 0)
            if returned >= limit or chars + needed > MAX_READ_CHARS:
                next_line = line_number
                break
            lines.append(rendered)
            chars += needed
            returned += 1

        if not lines:
            return "(empty file)" if offset == 1 else f"(no lines at or after {offset})"
        output = "\n".join(lines)
        if next_line is not None and (paged or requested_limit is None):
            output += f"\n... (more content; read_file offset={next_line})"
        elif long_line:
            output += (
                "\n... (a source line was shortened; use search_file for a keyword)"
            )
        return output

    def run_search(
        path: str,
        query: str,
        context: int = 2,
        max_matches: int = 20,
        root: str = "workspace",
    ) -> str:
        """Literal, bounded search intended for compacted transcript/output recall."""
        target = safe_path(read_root(root), path)
        if not isinstance(query, str) or not query or "\n" in query or "\r" in query:
            raise ValueError("query must be a non-empty single-line string")
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError(f"query must be at most {MAX_SEARCH_QUERY_CHARS} chars")
        if isinstance(context, bool) or not isinstance(context, int) or context < 0:
            raise ValueError("context must be a non-negative integer")
        if context > MAX_SEARCH_CONTEXT:
            raise ValueError(f"context must be at most {MAX_SEARCH_CONTEXT}")
        max_matches = _positive_int(max_matches, "max_matches", MAX_SEARCH_MATCHES)

        # The matcher keeps only a small suffix of a long line.  This both
        # detects a literal split across reads and gives every match a useful
        # preceding excerpt without loading an entire log line into memory.
        suffix_size = max(len(query) - 1, MATCH_EXCERPT_CHARS)
        previous: deque[tuple[int, str, bool]] = deque(maxlen=context)
        records: list[dict] = []
        source_bytes = 0
        partial_scan = False
        stopped_after_matches = False
        line_number = 0
        with target.open(encoding="utf-8") as handle:
            while first_fragment := handle.readline(SEARCH_CHUNK_CHARS):
                line_number += 1
                line_preview = ""
                line_was_long = not first_fragment.endswith("\n")
                suffix = ""
                candidates: list[dict] = []
                fragment = first_fragment

                while fragment:
                    source_bytes += len(fragment.encode("utf-8"))
                    if source_bytes > MAX_SEARCH_SOURCE_BYTES:
                        partial_scan = True
                        break
                    text = fragment.rstrip("\r\n")
                    if len(line_preview) < CONTEXT_LINE_CHARS:
                        line_preview += text[: CONTEXT_LINE_CHARS - len(line_preview)]

                    for candidate in candidates:
                        remaining = MATCH_EXCERPT_CHARS - len(candidate["after"])
                        if remaining > 0:
                            candidate["after"] += text[:remaining]

                    window = suffix + text
                    start = window.find(query)
                    while start >= 0:
                        end = start + len(query)
                        # A match entirely in suffix was handled by the prior
                        # fragment; only accept matches that consume new text.
                        if end > len(suffix) and len(candidates) < max_matches:
                            candidates.append(
                                {
                                    "before": window[
                                        max(0, start - MATCH_EXCERPT_CHARS) : start
                                    ],
                                    "after": window[end : end + MATCH_EXCERPT_CHARS],
                                }
                            )
                        start = window.find(query, start + 1)
                    suffix = window[-suffix_size:]

                    if fragment.endswith("\n"):
                        break
                    fragment = handle.readline(SEARCH_CHUNK_CHARS)
                    if not fragment:
                        break

                line_was_long = line_was_long or len(line_preview) >= CONTEXT_LINE_CHARS
                for candidate in candidates:
                    if len(records) >= max_matches:
                        break
                    records.append(
                        {
                            "before_context": list(previous),
                            "match_line": line_number,
                            "match": (candidate["before"] + query + candidate["after"]),
                            "match_was_long": line_was_long,
                            "after_context": [],
                        }
                    )

                for record in records:
                    if (
                        record["match_line"]
                        < line_number
                        <= record["match_line"] + context
                    ):
                        record["after_context"].append(
                            (line_number, line_preview, line_was_long)
                        )
                previous.append((line_number, line_preview, line_was_long))

                if partial_scan:
                    break

                if records and len(records) >= max_matches:
                    last_match = max(record["match_line"] for record in records)
                    if line_number >= last_match + context:
                        stopped_after_matches = True
                        break

        if not records:
            suffix = " (partial scan)" if partial_scan else ""
            return f"(no literal matches for {query!r}{suffix})"

        output: list[str] = []
        chars = 0
        for index, record in enumerate(records, start=1):
            if index > 1:
                output.append("")
            output.append(f"Match {index}:")
            entries = (
                [(*entry, False) for entry in record["before_context"]]
                + [
                    (
                        record["match_line"],
                        record["match"],
                        record["match_was_long"],
                        True,
                    )
                ]
                + [(*entry, False) for entry in record["after_context"]]
            )
            for match_line, text, source_was_long, is_match in entries:
                clipped = text[:CONTEXT_LINE_CHARS]
                if len(text) > len(clipped) or source_was_long:
                    clipped += " ... (line truncated)"
                rendered = f"{'> ' if is_match else '  '}{match_line}: {clipped}"
                needed = len(rendered) + 1
                if chars + needed > MAX_SEARCH_CHARS:
                    output.append(
                        "... (search output truncated; narrow query or context)"
                    )
                    return "\n".join(output)
                output.append(rendered)
                chars += needed
        if stopped_after_matches:
            output.append(f"... (stopped after {max_matches} matches; narrow query)")
        if partial_scan:
            output.append("... (stopped after bounded source scan; refine the path)")
        return "\n".join(output)

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

    def run_glob(pattern: str, root: str = "workspace") -> str:
        directory = read_root(root)
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("glob pattern must be relative and cannot contain '..'")
        # os.walk does not recurse into symlink directories; glob's recursive
        # implementation does. Reject such directories before descending.
        matches = []
        scanned = 0
        for current, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
            for name in [*dirs, *files]:
                scanned += 1
                if scanned > 50_000:
                    break
                candidate = Path(current) / name
                rel = candidate.relative_to(directory).as_posix()
                if not candidate.resolve().is_relative_to(directory):
                    continue
                if _glob_matches(rel, pattern):
                    matches.append(rel)
                    if len(matches) >= 501:
                        break
            if len(matches) >= 501 or scanned > 50_000:
                break
        matches.sort()
        if scanned > 50_000:
            return "\n".join(matches + ["... (partial scan; refine root or pattern)"])
        if not matches:
            return "(no matches)"
        if len(matches) > 500:
            matches = matches[:500] + [
                "... (stopped after 500 matches; narrow pattern)"
            ]
        return "\n".join(matches)

    def run_todo(todos) -> str:
        output = todo.update(todos)
        return output

    box.add(
        "bash",
        "Run a shell command in the workspace. Return exit code, bounded preview "
        "and local stdout/stderr paths; read/search those files for full evidence. "
        "Timeout/cancellation preserve partial output. Not a shell sandbox.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            "required": ["command"],
        },
        run_bash,
    )
    box.add(
        "read_file",
        "Read a bounded page from a text file inside the workspace. "
        "offset is a 1-based line number. Archives in .task_outputs/tool-results "
        "or .transcripts instead use char_offset/max_chars; follow next_char_offset.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer", "minimum": 1},
                "char_offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            "required": ["path"],
        },
        run_read,
    )
    box.add(
        "search_file",
        "Find literal text in a workspace file and return matching line numbers "
        "with bounded surrounding context. Use this to recall compacted outputs.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string", "minLength": 1},
                "context": {"type": "integer", "minimum": 0, "maximum": 10},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["path", "query"],
        },
        run_search,
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
        "Plan once for multi-step tasks; update only when a step changes. "
        "Every item needs nonempty content; at most one may be in_progress.",
        {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["todos"],
        },
        run_todo,
    )
    for name in ("read_file", "search_file", "glob"):
        definition = box._tools[name]
        definition.input_schema["properties"]["root"] = {
            "type": "string",
            "enum": list(roots),
            "default": "workspace",
        }
        definition.description += " Select root: " + ", ".join(roots) + "."
    return (box if include is None else box.select(include)), todo
