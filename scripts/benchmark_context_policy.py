"""Repeatable, offline context-policy replay.

Run from the repository root:
    python scripts/benchmark_context_policy.py

It compares commit 5154aa2 with the checked-out implementation.  The replay
uses fixed local clients, so it measures observable control-flow and character
counts only; it does not claim a model-quality, token-cost, or latency result.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop.compact import Compactor  # noqa: E402
from agentloop.models import ModelError, ModelResponse  # noqa: E402

BASELINE_REVISION = "5154aa2"
CHAR_LIMIT = 50_000


def _pair(call_id: str, output: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "read_file",
                    "input": {"path": "fixture.log"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": output}
            ],
        },
    ]


class FixedSummary:
    """A local summary client which records its requests."""

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, system: str, messages: list, tools: list):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages)})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelResponse(
            text=reply,
            blocks=[{"type": "text", "text": reply}],
            usage={"input_tokens": 0, "output_tokens": 0},
        )


def _load_baseline(temp: Path):
    """Load old relative-import modules as agentloop._baseline.*.

    Loading under a real package name and registering each module before exec is
    required both for ``from .models`` and for dataclasses' module lookup.
    """

    temp.mkdir(parents=True, exist_ok=True)
    package_name = "agentloop._baseline"
    package = types.ModuleType(package_name)
    package.__path__ = [str(temp)]
    sys.modules[package_name] = package
    for name in ("models", "compact"):
        source = subprocess.check_output(
            ["git", "show", f"{BASELINE_REVISION}:agentloop/{name}.py"], cwd=ROOT
        )
        path = temp / f"{name}.py"
        path.write_bytes(source)
        qualified = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(qualified, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load baseline {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.compact"], sys.modules[f"{package_name}.models"]


def _chars(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, default=str))


def _checkpoint(messages: list[dict]) -> dict:
    content = messages[0]["content"]
    return json.loads(content.split("\n", 1)[1])


def _full_output_path(messages: list[dict]) -> str:
    text = messages[-1]["content"][0]["content"]
    return text.rsplit("Full output: ", 1)[1].strip()


def _scenario_latest_tool_result(baseline_compactor, temp: Path) -> dict:
    target_output_chars = 120_000
    tail = "TAIL_ERROR: volume mount failed"
    prefix = "BEGIN\n"
    result = prefix + "x" * (target_output_chars - len(prefix) - len(tail)) + tail
    assert len(result) == target_output_chars
    messages = [{"role": "user", "content": "inspect only"}, *_pair("new", result)]
    rows = {}
    for label, cls in (("baseline", baseline_compactor), ("current", Compactor)):
        client = FixedSummary(["fixed summary"])
        workdir = temp / "latest" / label
        out = cls(workdir, client=client, char_limit=CHAR_LIMIT).prepare(messages)
        rendered = json.dumps(out, ensure_ascii=False)
        row = {
            "output_chars": len(result),
            "before_chars": _chars(messages),
            "after_chars": _chars(out),
            "within_char_limit": _chars(out) <= CHAR_LIMIT,
            "summary_calls": len(client.calls),
            "tail_error_visible": tail in rendered,
            "output_archived": False,
            "original_output_round_trip": None,
        }
        if label == "current":
            path = _full_output_path(out)
            restored = (workdir / path).read_text(encoding="utf-8")
            row["output_archived"] = True
            row["original_output_round_trip"] = restored == result
            assert row["within_char_limit"]
            assert row["summary_calls"] == 0
            assert row["tail_error_visible"]
            assert row["original_output_round_trip"]
        rows[label] = row
    return rows


def _scenario_summary_sampling(baseline_compactor, temp: Path) -> dict:
    middle = "MIDDLE_ERROR: request denied"
    tail = "TAIL_MARKER: inspect this final line"
    history = "prefix\n" + "x" * 1_000 + middle + "\n" + "y" * 8_000 + tail
    messages = [
        {"role": "user", "content": "diagnose"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "sampling-call",
                    "name": "read_file",
                    "input": {"path": "fixture.log"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "sampling-call",
                    "content": history,
                }
            ],
        },
        *[{"role": "assistant", "content": f"recent-{i}"} for i in range(6)],
    ]
    rows = {}
    for label, cls in (("baseline", baseline_compactor), ("current", Compactor)):
        client = FixedSummary(["fixed summary"])
        cls(temp / "sampling" / label, client=client, char_limit=1_000).prepare(
            messages
        )
        rendered = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
        rows[label] = {
            "summary_calls": len(client.calls),
            "middle_error_in_summary_input": middle in rendered,
            "tail_marker_in_summary_input": tail in rendered,
        }
    assert rows["current"]["middle_error_in_summary_input"]
    assert rows["current"]["tail_marker_in_summary_input"]
    assert not rows["baseline"]["middle_error_in_summary_input"]
    assert not rows["baseline"]["tail_marker_in_summary_input"]
    return rows


def _scenario_request_ledger(baseline_compactor, temp: Path) -> dict:
    read_only = "只读检查，不写入文件"
    source = [
        {"role": "user", "content": read_only},
        {"role": "assistant", "content": "x" * 60_000},
    ]
    rows = {}
    for label, cls in (("baseline", baseline_compactor), ("current", Compactor)):
        client = FixedSummary(
            [
                "summary deliberately omits the read-only constraint",
                "second summary also deliberately omits the read-only constraint",
            ]
        )
        compactor = cls(temp / "ledger" / label, client=client, char_limit=CHAR_LIMIT)
        first = compactor.summarize(source, current_request=read_only)
        # Both fixed summaries omit the constraint. A second explicit compaction
        # ensures this scenario does not merely inherit the first checkpoint.
        second = [
            *first,
            {"role": "user", "content": "继续", "_request_id": "continue-1"},
            {"role": "assistant", "content": "new history"},
        ]
        out = compactor.summarize(second, current_request="继续")
        state = _checkpoint(out)
        rows[label] = {
            "summary_contains_read_only": read_only in state.get("summary", ""),
            "context_contains_read_only": read_only
            in json.dumps(out, ensure_ascii=False),
            "has_user_requests_field": "user_requests" in state,
            "ledger_contains_read_only": any(
                item.get("text") == read_only for item in state.get("user_requests", [])
            ),
        }
    assert len(client.calls) == 2
    assert not rows["current"]["summary_contains_read_only"]
    assert rows["current"]["context_contains_read_only"]
    assert rows["current"]["ledger_contains_read_only"]
    return rows


def _scenario_failed_incremental_summary(temp: Path) -> dict:
    old = "read-only original constraint"
    failure = ModelError("injected offline summary failure")
    client = FixedSummary(["summary-v1", failure, "summary-v2"])
    compactor = Compactor(temp / "pending", client=client, char_limit=CHAR_LIMIT)
    first = compactor.summarize(
        [
            {"role": "user", "content": old},
            {"role": "assistant", "content": "old history"},
        ],
        current_request=old,
    )
    failed = compactor.summarize(
        [
            *first,
            {"role": "user", "content": "continue", "_request_id": "continue-2"},
            {"role": "assistant", "content": "new history that must replay"},
        ],
        current_request="continue",
    )
    failed_state = _checkpoint(failed)
    assert failed_state["summary"] == "summary-v1"
    assert failed_state["pending_transcripts"]
    recovered = compactor.summarize(failed, current_request="continue")
    recovered_state = _checkpoint(recovered)
    replay_payload = json.dumps(client.calls[2]["messages"], ensure_ascii=False)
    row = {
        "summary_preserved_after_failure": failed_state["summary"] == "summary-v1",
        "pending_count_after_failure": len(failed_state["pending_transcripts"]),
        "pending_cleared_after_replay": recovered_state["pending_transcripts"] == [],
        "failed_delta_replayed_into_next_summary_input": "new history that must replay"
        in replay_payload,
    }
    assert row["pending_cleared_after_replay"]
    assert row["failed_delta_replayed_into_next_summary_input"]
    return {"current": row, "baseline": {"pending_replay_state": "not implemented"}}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentloop-context-policy-") as raw:
        temp = Path(raw)
        baseline_compact, _baseline_models = _load_baseline(temp / "baseline")
        report = {
            "baseline_revision": BASELINE_REVISION,
            "measurement": "serialized message character count, not tokens",
            "execution": (
                "offline fixed Mock-style responses; no network or model-quality claim"
            ),
            "scenarios": {
                "latest_120k_tool_result": _scenario_latest_tool_result(
                    baseline_compact.Compactor, temp
                ),
                "summary_sampling": _scenario_summary_sampling(
                    baseline_compact.Compactor, temp
                ),
                "read_only_then_continue": _scenario_request_ledger(
                    baseline_compact.Compactor, temp
                ),
                "failed_incremental_summary": _scenario_failed_incremental_summary(
                    temp
                ),
            },
        }
    target = ROOT / "docs/evidence/context-policy-replay.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
