from loopsmith.tools import TodoManager, build_toolbox, safe_path


def _run(box, name, **kwargs):
    return box.execute({"name": name, "input": kwargs})


def test_write_read_roundtrip(workdir):
    box, _ = build_toolbox(workdir)
    assert "Wrote 5 bytes" in _run(box, "write_file", path="a.txt", content="hello")
    assert _run(box, "read_file", path="a.txt") == "hello"


def test_read_limit(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="n.txt", content="\n".join(f"line{i}" for i in range(10)))
    assert _run(box, "read_file", path="n.txt", limit=3).splitlines() == ["line0", "line1", "line2"]


def test_edit_replaces_first_occurrence(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="e.txt", content="aa bb aa")
    out = _run(box, "edit_file", path="e.txt", old_text="aa", new_text="cc")
    assert "first of 2 occurrences" in out
    assert _run(box, "read_file", path="e.txt") == "cc bb aa"


def test_edit_missing_text_returns_error(workdir):
    box, _ = build_toolbox(workdir)
    _run(box, "write_file", path="e.txt", content="hello")
    assert _run(box, "edit_file", path="e.txt", old_text="nope", new_text="x").startswith("Error:")


def test_path_escape_is_blocked(workdir):
    box, _ = build_toolbox(workdir)
    out = _run(box, "write_file", path="../evil.txt", content="x")
    assert out.startswith("Error:")
    assert not (workdir.parent / "evil.txt").exists()
    out2 = _run(box, "write_file", path="/tmp/loopsmith-evil.txt", content="x")
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
    out = todo.update([
        {"content": "step one", "status": "completed"},
        {"content": "step two", "status": "in_progress"},
        {"content": "step three", "status": "pending"},
    ])
    assert "[x] step one" in out
    assert "[>] step two" in out
    assert "[ ] step three" in out


def test_todo_only_one_in_progress():
    todo = TodoManager()
    try:
        todo.update([
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ])
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
