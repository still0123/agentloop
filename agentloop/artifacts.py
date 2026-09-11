"""Original artifact references carried outside model-authored text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ToolOutput(str):
    """A normal textual tool result with optional program-owned provenance."""

    def __new__(cls, content: str, artifact: dict):
        obj = super().__new__(cls, content)
        obj.artifact = dict(artifact)
        return obj


def artifact_path(workdir: Path, reference: dict) -> Path:
    root = Path(workdir).resolve()
    path = (root / reference["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("artifact outside workspace or unavailable")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("artifact exceeds bounded reader")
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("artifact changed since the observation")
    return path


def read_artifact(
    workdir: Path, path: Path, offset: int = 0, limit: int = 6000
) -> ToolOutput:
    """Read a character range without line-prefix wrapping or silent clipping."""
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 12000
    ):
        raise ValueError("invalid artifact character range")
    root = Path(workdir).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("artifact outside workspace or unavailable")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("artifact exceeds bounded reader")
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if offset > len(text):
        raise ValueError("artifact offset is beyond end of content")
    end = min(len(text), offset + limit)
    reference = {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return ToolOutput(
        json.dumps(
            {
                "artifact": reference["path"],
                "char_offset": offset,
                "next_char_offset": end if end < len(text) else None,
                "total_chars": len(text),
                "text": text[offset:end],
            },
            ensure_ascii=False,
        ),
        {**reference, "start": offset, "end": end},
    )


def wire_messages(messages: list) -> list:
    """Keep internal provenance in history, never send it as protocol fields."""
    result = []
    for message in messages:
        item = {k: v for k, v in message.items() if not k.startswith("_")}
        if isinstance(item.get("content"), list):
            item["content"] = [
                {k: v for k, v in block.items() if not k.startswith("_")}
                for block in item["content"]
            ]
        result.append(item)
    return result
