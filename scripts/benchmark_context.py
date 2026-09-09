"""Offline before/after replay. Fixed summaries measure mechanics, not LLM quality.

Run from the repository root: python scripts/benchmark_context.py
The baseline is the pre-change repository implementation at 16e0a07.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentloop.compact import Compactor  # noqa: E402
from agentloop.models import MockClient, ModelError  # noqa: E402


def pair(i, text):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": text}
            ],
        },
    ]


class Unavailable:
    def complete(self, *args, **kwargs):
        raise ModelError("offline injected summary failure")


def main():
    root = Path(__file__).resolve().parents[1]
    rows = []
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        baseline = temp / "baseline.py"
        baseline.write_bytes(
            subprocess.check_output(
                ["git", "show", "16e0a07:agentloop/compact.py"], cwd=root
            )
        )
        spec = importlib.util.spec_from_file_location("baseline", baseline)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tools = [{"role": "user", "content": "Diagnose the logs"}]
        for i in range(14):
            tools += pair(i, f"EVIDENCE_{i}:" + "log line\n" * 1000)
        changed = [
            {"role": "user", "content": "Fix the issue"},
            {"role": "assistant", "content": "history " * 10000},
            {"role": "user", "content": "Only inspect. DO_NOT_WRITE."},
        ]
        for scenario, messages in [
            ("old_tool_results", tools),
            ("changed_request", changed),
            ("summary_failure", changed),
        ]:
            for name, cls in [("before", module.Compactor), ("after", Compactor)]:
                workdir = temp / scenario / name
                client = (
                    Unavailable()
                    if scenario == "summary_failure"
                    else MockClient(
                        ["Investigate the issue; review existing evidence."]
                    )
                )
                compactor = cls(workdir, client=client)
                before = len(json.dumps(messages, ensure_ascii=False))
                started = perf_counter()
                try:
                    out = compactor.prepare(copy.deepcopy(messages))
                    rendered = json.dumps(out, ensure_ascii=False)
                    after = len(rendered)
                    files = list(workdir.rglob("*")) if workdir.exists() else []
                    artifacts = [p for p in files if p.is_file()]
                    archive_text = "\n".join(p.read_text() for p in artifacts)
                    rows.append(
                        {
                            "scenario": scenario,
                            "version": name,
                            "status": "ok",
                            "before_chars": before,
                            "after_chars": after,
                            "reduction_percent": round((1 - after / before) * 100, 2),
                            "summary_calls": len(client.calls)
                            if hasattr(client, "calls")
                            else 1,
                            "latest_request_verbatim": "DO_NOT_WRITE." in rendered
                            if scenario != "old_tool_results"
                            else None,
                            "old_evidence_retained_or_archived": "EVIDENCE_0:"
                            in rendered
                            or "EVIDENCE_0:" in archive_text
                            if scenario == "old_tool_results"
                            else None,
                            "artifact_bytes": sum(p.stat().st_size for p in artifacts),
                            "local_elapsed_ms": round(
                                (perf_counter() - started) * 1000, 2
                            ),
                        }
                    )
                except ModelError as exc:
                    rows.append(
                        {
                            "scenario": scenario,
                            "version": name,
                            "status": type(exc).__name__,
                            "before_chars": before,
                        }
                    )
    print(
        json.dumps(
            {
                "baseline": "16e0a07",
                "measurement": "JSON character count, not tokens",
                "summary_mode": "fixed response; no semantic quality evaluation",
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
