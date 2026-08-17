"""压缩管线测试 —— LoopSmith 的深挖机制，重点验证四步顺序与配对不变量。"""

from loopsmith.compact import Compactor, _has_tool_use, _is_tool_result_msg
from loopsmith.models import MockClient


def _pair(tid, result_text):
    return (
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": tid, "name": "bash", "input": {}}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": tid, "content": result_text}]},
    )


def _assert_pairing_intact(messages):
    """不变量：每条 tool_result 消息的前一条必须是带 tool_use 的 assistant。"""
    for i, msg in enumerate(messages):
        if _is_tool_result_msg(msg):
            assert i > 0 and _has_tool_use(messages[i - 1]), (
                f"orphan tool_result at index {i}"
            )


def test_spill_writes_disk_and_keeps_path(workdir):
    compactor = Compactor(workdir, client=None, batch_budget=20_000, spill_threshold=30_000)
    messages = [{"role": "user", "content": "q"}, *_pair("t1", "x" * 40_000)]
    out = compactor.prepare(messages)
    content = out[-1]["content"][0]["content"]
    assert "Full output: " in content and ".task_outputs/tool-results/t1.txt" in content
    assert len(content) < 5_000  # 只剩预览 + 路径
    spilled = workdir / ".task_outputs" / "tool-results" / "t1.txt"
    assert spilled.read_text() == "x" * 40_000  # 完整内容可找回


def test_spill_skips_when_under_budget(workdir):
    compactor = Compactor(workdir, client=None, batch_budget=200_000, spill_threshold=30_000)
    messages = [{"role": "user", "content": "q"}, *_pair("t1", "x" * 40_000)]
    out = compactor.prepare(messages)
    assert out[-1]["content"][0]["content"] == "x" * 40_000
    assert not (workdir / ".task_outputs").exists()


def test_snip_archives_and_keeps_size(workdir):
    compactor = Compactor(workdir, client=None)
    messages = [{"role": "user", "content": "q"}] + [
        {"role": "assistant" if i % 2 else "user", "content": f"m{i}"}
        for i in range(60)
    ]
    out = compactor._snip(messages)
    assert len(out) <= 51  # max_messages(50) + 1 个归档标记
    assert any(
        isinstance(m.get("content"), str) and "messages archived at" in m["content"]
        for m in out
    )
    assert list((workdir / ".transcripts").glob("transcript-*.json"))  # 完整历史落盘


def test_snip_head_boundary_protects_pair(workdir):
    """头边界若切在 tool_use 之后，其 tool_result 必须一起保留。"""
    compactor = Compactor(workdir, client=None)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "thinking"},
        *_pair("t1", "r1"),  # 落在 index 2/3，head_end=3 正好切在中间
    ] + [
        {"role": "assistant" if i % 2 else "user", "content": f"f{i}"} for i in range(55)
    ]
    out = compactor._snip(messages)
    _assert_pairing_intact(out)


def test_snip_tail_boundary_protects_pair(workdir):
    """尾边界若落在 tool_result 上且其 tool_use 在界外，边界前移。"""
    compactor = Compactor(workdir, client=None)
    messages = (
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        + [{"role": "user", "content": f"f{i}"} for i in range(2, 8)]  # index 2..7
        + list(_pair("t9", "r9"))  # 恰好落在 index 8/9；tail_start=9 正好切在配对中间
        + [{"role": "user", "content": f"b{i}"} for i in range(46)]
    )
    assert len(messages) == 56
    out = compactor._snip(messages)
    _assert_pairing_intact(out)
    # 该配对必须整体保留在尾部
    assert any(
        isinstance(m.get("content"), list) and any(
            b.get("tool_use_id") == "t9" for b in m["content"]
        )
        for m in out
    )


def test_placeholder_replaces_old_but_not_new(workdir):
    compactor = Compactor(workdir, client=None, keep_recent_results=1, placeholder_limit=100)
    messages = (
        [{"role": "user", "content": "q"}]
        + list(_pair("t1", "a" * 300))
        + list(_pair("t2", "b" * 300))
        + list(_pair("t3", "c" * 300))   # 最近一条已读 → 保留
        + list(_pair("t4", "d" * 500))   # 最新批次（未读）→ 必须完整
    )
    out = compactor.prepare(messages)
    contents = [
        b["content"]
        for m in out
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]
    assert "[Earlier tool result omitted.]" in contents[0]
    assert "[Earlier tool result omitted.]" in contents[1]
    assert contents[2] == "c" * 300
    assert contents[3] == "d" * 500


def test_placeholder_keeps_spill_path(workdir):
    compactor = Compactor(workdir, client=None, keep_recent_results=0, placeholder_limit=100)
    marked = "head\n\nFull output: .task_outputs/tool-results/t0.txt\n" + "x" * 200
    messages = [
        {"role": "user", "content": "q"},
        *_pair("t0", marked),
        *_pair("t1", "newest"),
    ]
    out = compactor.prepare(messages)
    old = out[2]["content"][0]["content"]
    assert old == "[Earlier tool result saved at .task_outputs/tool-results/t0.txt]"


def test_summarize_replaces_history(workdir):
    summarizer = MockClient(["SUMMARY FACTS"])
    compactor = Compactor(workdir, client=summarizer, char_limit=1_000)
    messages = [
        {"role": "user", "content": "original request"},
        {"role": "assistant", "content": "a" * 2_000},
        {"role": "user", "content": "b" * 2_000},
    ]
    out = compactor.prepare(messages)
    assert len(out) == 1
    content = out[0]["content"]
    assert "[Compacted]" in content
    assert "SUMMARY FACTS" in content
    assert "original request" in content  # 当前请求与摘要明确分开
    assert "Full transcript:" in content
    assert list((workdir / ".transcripts").glob("transcript-*.json"))


def test_summarize_skipped_under_limit(workdir):
    summarizer = MockClient(["SHOULD NOT BE CALLED"])
    compactor = Compactor(workdir, client=summarizer, char_limit=1_000_000)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "short"},
    ]
    out = compactor.prepare(messages)
    assert out == messages
    assert summarizer.calls == []


def test_reactive_compact_keeps_recent(workdir):
    compactor = Compactor(workdir, client=MockClient(["REACTIVE"]))
    messages = [{"role": "user", "content": "q"}] + [
        {"role": "assistant" if i % 2 else "user", "content": f"m{i}"} for i in range(12)
    ]
    out = compactor.reactive_compact(messages)
    assert out[0]["content"].startswith("[Reactive compact]")
    assert "REACTIVE" in out[0]["content"]
    assert len(out) <= 6  # 摘要 + 最多 5 条最近消息
    _assert_pairing_intact(out)


def test_reactive_compact_pairing_guard(workdir):
    compactor = Compactor(workdir, client=MockClient(["R"]))
    messages = (
        [{"role": "user", "content": "q"}]
        + [{"role": "user", "content": f"f{i}"} for i in range(4)]
        + list(_pair("tX", "rx"))
        + [{"role": "user", "content": f"b{i}"} for i in range(3)]
        + [{"role": "user", "content": "tail"}]
    )
    out = compactor.reactive_compact(messages)
    _assert_pairing_intact(out)
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("tool_use_id") == "tX" for b in m["content"])
        for m in out
    )
