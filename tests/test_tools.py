import threading
import time

import agentloop.tools as tools_module
from agentloop.tools import TodoManager, build_toolbox, safe_path


def _run(box, name, **kwargs):
    return box.execute({"name": name, "input": kwargs})


def test_write_read_roundtrip(workdir):
    box, _ = build_toolbox(workdir)
    assert "Wrote 5 bytes" in _run(box, "write_file", path="a.txt", content="hello")
    assert _run(box, "read_file", path="a.txt") == "hello"


def test_read_limit(workdir):
    box, _ = build_toolbox(workdir)
    _run(
        box,
        "write_file",
        path="n.txt",
        content="\n".join(f"line{i}" for i in range(10)),
    )
    assert _run(box, "read_file", path="n.txt", limit=3).splitlines() == [
        "line0",
        "line1",
        "line2",
    ]


def test_read_offset_is_one_based_and_returns_next_page_hint(workdir):
    box, _ = build_toolbox(workdir)
    _run(
        box,
        "write_file",
        path="n.txt",
        content="\n".join(f"line{i}" for i in range(5)),
    )

    out = _run(box, "read_file", path="n.txt", offset=3, limit=2)

    assert out.splitlines() == [
        "line2",
        "line3",
        "... (more content; read_file offset=5)",
    ]


def test_read_rejects_invalid_pagination_values(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="n.txt", content="line")
    assert _run(box, "read_file", path="n.txt", offset=0).startswith("Error:")
    assert _run(box, "read_file", path="n.txt", limit=0).startswith("Error:")


def test_search_file_returns_literal_matches_with_line_numbers_and_context(workdir):
    box, _ = build_toolbox(workdir)
    _run(
        box,
        "write_file",
        path="logs/agent.log",
        content=(
            "start\nconnection failed [code=42]\nretrying\n"
            "connection failed [code=42]\ndone"
        ),
    )

    out = _run(
        box,
        "search_file",
        path="logs/agent.log",
        query="[code=42]",
        context=1,
    )

    assert "Match 1:" in out
    assert "> 2: connection failed [code=42]" in out
    assert "> 4: connection failed [code=42]" in out
    assert "  1: start" in out
    assert "  5: done" in out


def test_search_file_finds_needle_at_the_end_of_a_long_line(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="long.txt", content=("x" * 20_000) + "needle")

    out = _run(box, "search_file", path="long.txt", query="needle")

    assert "> 1:" in out
    assert "needle" in out
    assert len(out) < 1_000


def test_search_file_finds_a_literal_split_across_read_chunks(workdir):
    box, _ = build_toolbox(workdir)
    _run(
        box,
        "write_file",
        path="split.txt",
        content=("x" * (tools_module.SEARCH_CHUNK_CHARS - 2)) + "needle",
    )

    out = _run(box, "search_file", path="split.txt", query="needle")

    assert "> 1:" in out
    assert "needle" in out


def test_search_file_bounds_actual_source_scan(workdir, monkeypatch):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="large.txt", content=("x" * 200) + "\nneedle")
    monkeypatch.setattr(tools_module, "MAX_SEARCH_SOURCE_BYTES", 100)

    out = _run(box, "search_file", path="large.txt", query="needle")

    assert "no literal matches" in out
    assert "partial scan" in out


def test_search_file_stops_after_max_matches_and_needed_context(workdir, monkeypatch):
    box, _ = build_toolbox(workdir)
    _run(
        box,
        "write_file",
        path="many.txt",
        content="needle\nneedle\n" + ("x" * 1_000),
    )
    monkeypatch.setattr(tools_module, "MAX_SEARCH_SOURCE_BYTES", 100)

    out = _run(
        box, "search_file", path="many.txt", query="needle", max_matches=2, context=0
    )

    assert out.count("Match ") == 2
    assert "stopped after 2 matches" in out
    assert "partial scan" not in out


def test_search_file_protects_workspace_paths(workdir):
    box, _ = build_toolbox(workdir)
    assert _run(box, "search_file", path="../outside", query="x").startswith("Error:")


def test_search_keeps_found_matches_when_source_limit_interrupts_long_line(
    workdir, monkeypatch
):
    import agentloop.tools as tools_module

    monkeypatch.setattr(tools_module, "MAX_SEARCH_SOURCE_BYTES", 9000)
    (workdir / "long.txt").write_text("needle" + "x" * 20000)
    box, _ = build_toolbox(workdir)
    out = _run(box, "search_file", path="long.txt", query="needle")
    assert "needle" in out
    assert "Match 1:" in out
    assert "bounded source scan" in out


def test_search_rejects_unbounded_literal(workdir):
    box, _ = build_toolbox(workdir)
    out = _run(box, "search_file", path="any.txt", query="x" * 100000)
    assert out.startswith("Error:")
    assert "query must be at most" in out


def test_edit_replaces_first_occurrence(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="e.txt", content="aa bb aa")
    out = _run(box, "edit_file", path="e.txt", old_text="aa", new_text="cc")
    assert "first of 2 occurrences" in out
    assert _run(box, "read_file", path="e.txt") == "cc bb aa"


def test_edit_missing_text_returns_error(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="e.txt", content="hello")
    assert _run(
        box, "edit_file", path="e.txt", old_text="nope", new_text="x"
    ).startswith("Error:")


def test_path_escape_is_blocked(workdir):
    box, _ = build_toolbox(workdir)
    out = _run(box, "write_file", path="../evil.txt", content="x")
    assert out.startswith("Error:")
    assert not (workdir.parent / "evil.txt").exists()
    out2 = _run(box, "write_file", path="/tmp/agentloop-evil.txt", content="x")
    assert out2.startswith("Error:")


def test_safe_path_allows_absolute_inside_workdir(workdir):
    target = workdir / "sub" / "f.txt"
    target.parent.mkdir()
    assert safe_path(workdir, str(target)) == target.resolve()


def test_glob_finds_files(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="src/a.py", content="x")
    _run(box, "write_file", path="src/b.py", content="x")
    out = _run(box, "glob", pattern="**/*.py")
    assert "src/a.py" in out and "src/b.py" in out


def test_bash_exit_code_and_output(workdir):
    box, _ = build_toolbox(workdir)
    out = _run(box, "bash", command="echo hi")
    assert "exit=0" in out and "hi" in out
    out2 = _run(box, "bash", command="exit 3")
    assert "exit=3" in out2


def test_bash_can_be_cancelled(workdir):
    cancelled = threading.Event()
    box, _ = build_toolbox(workdir, should_stop=cancelled.is_set)
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    started = time.monotonic()

    out = _run(box, "bash", command="sleep 10")

    timer.join()
    assert "cancelled by user" in out
    assert time.monotonic() - started < 3


def test_unknown_tool(workdir):
    box, _ = build_toolbox(workdir)
    assert "unknown tool" in _run(box, "nope", x=1)


def test_bad_arguments_return_error(workdir):
    box, _ = build_toolbox(workdir)
    out = _run(box, "read_file")  # 缺 path
    assert out.startswith("Error: bad arguments")


def test_toolbox_rejects_duplicate_names(workdir):
    box, _ = build_toolbox(workdir)
    try:
        box.add("bash", "dup", {}, lambda: "")
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------


def test_todo_render_marks():
    todo = TodoManager()
    out = todo.update(
        [
            {"content": "step one", "status": "completed"},
            {"content": "step two", "status": "in_progress"},
            {"content": "step three", "status": "pending"},
        ]
    )
    assert "[x] step one" in out
    assert "[>] step two" in out
    assert "[ ] step three" in out


def test_todo_only_one_in_progress():
    todo = TodoManager()
    try:
        todo.update(
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        )
    except ValueError as exc:
        assert "in_progress" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_todo_limits(workdir):
    box, todo = build_toolbox(workdir)
    many = [{"content": f"item{i}", "status": "pending"} for i in range(21)]
    assert _run(box, "todo_write", todos=many).startswith("Error:")
    empty = _run(box, "todo_write", todos=[{"content": "  ", "status": "pending"}])
    assert empty.startswith("Error:")
