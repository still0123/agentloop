import shlex
import sys

from agentloop.tools import build_toolbox


def shell(box, source, **kwargs):
    return box.execute(
        {
            "name": "bash",
            "input": {
                "command": shlex.join([sys.executable, "-c", source]),
                **kwargs,
            },
        }
    )


def test_long_command_output_can_be_recalled_after_preview(workdir):
    box, _ = build_toolbox(workdir)
    output = shell(
        box,
        "print('before\\n' * 30000); print('MIDDLE_EVIDENCE'); "
        "print('after\\n' * 30000)",
    )
    assert "MIDDLE_EVIDENCE" not in output
    assert len(output) < 20000
    path = next((workdir / ".task_outputs/commands").glob("*.stdout.txt"))
    assert path.stat().st_size > 200000
    assert path.stat().st_mode & 0o777 == 0o600
    recalled = box.execute(
        {
            "name": "search_file",
            "input": {
                "path": str(path),
                "query": "MIDDLE_EVIDENCE",
            },
        }
    )
    assert "MIDDLE_EVIDENCE" in recalled
    assert "30002:" in recalled


def test_timeout_retains_partial_output(workdir):
    box, _ = build_toolbox(workdir)
    output = shell(
        box,
        "import time; print('before-timeout', flush=True); time.sleep(5)",
        timeout=1,
    )
    assert output.startswith("Error: command timed out")
    assert "before-timeout" in output
    assert "incomplete" in output
    assert (
        "before-timeout"
        in next((workdir / ".task_outputs/commands").glob("*.stdout.txt")).read_text()
    )


def test_output_limit_stops_infinite_writer_and_labels_partial(workdir, monkeypatch):
    monkeypatch.setattr("agentloop.command.MAX_CAPTURE_BYTES", 4096)
    box, _ = build_toolbox(workdir)
    output = shell(box, "import os\nwhile True: os.write(1, b'x' * 8192)")
    assert "output limit exceeded" in output
    assert "partial" in output
    path = next((workdir / ".task_outputs/commands").glob("*.stdout.txt"))
    assert path.stat().st_size == 4096


def test_bad_timeout_cannot_start_process(workdir):
    box, _ = build_toolbox(workdir)
    for timeout in (0, -1, 601, True, "60"):
        assert shell(box, "print('never')", timeout=timeout).startswith("Error:")
    assert not (workdir / ".task_outputs/commands").exists()


def test_reusable_read_tools_cannot_write_or_escape_roots(workdir, tmp_path_factory):
    source = tmp_path_factory.mktemp("external_source")
    (source / "facts.txt").write_text("reference evidence")
    (workdir / "external").symlink_to(source, target_is_directory=True)
    box, _ = build_toolbox(
        workdir,
        include=("read_file", "search_file", "glob", "todo_write"),
        read_roots={"source": source},
    )
    assert "unknown tool" in box.execute({"name": "bash", "input": {}})
    assert "unknown tool" in box.execute({"name": "write_file", "input": {}})
    assert (
        box.execute(
            {
                "name": "read_file",
                "input": {
                    "path": "facts.txt",
                    "root": "source",
                },
            }
        )
        == "reference evidence"
    )
    assert "Error:" in box.execute(
        {
            "name": "read_file",
            "input": {
                "path": "external/facts.txt",
            },
        }
    )
    assert "facts.txt" not in box.execute(
        {"name": "glob", "input": {"pattern": "**/*"}}
    )
    assert "Error:" in box.execute({"name": "glob", "input": {"pattern": "../**/*"}})


def test_glob_pattern_respects_directory_depth(workdir):
    (workdir / "a.txt").write_text("a")
    (workdir / "child").mkdir()
    (workdir / "child/b.txt").write_text("b")
    box, _ = build_toolbox(workdir)
    assert box.execute({"name": "glob", "input": {"pattern": "*.txt"}}) == "a.txt"
    assert "child/b.txt" in box.execute(
        {"name": "glob", "input": {"pattern": "**/*.txt"}}
    )


def test_named_read_root_does_not_grant_write_access(workdir, tmp_path_factory):
    external = tmp_path_factory.mktemp("read_only")
    target = external / "source.txt"
    target.write_text("original")
    box, _ = build_toolbox(workdir, read_roots={"reference": external})
    result = box.execute(
        {
            "name": "write_file",
            "input": {
                "path": str(target),
                "content": "changed",
            },
        }
    )
    assert result.startswith("Error:")
    assert target.read_text() == "original"


def test_tool_composition_rejects_duplicates_without_partial_changes(workdir):
    import pytest

    from agentloop.tools import Toolbox

    core, _ = build_toolbox(workdir, include=("read_file", "glob"))
    app = Toolbox()
    app.add("submit", "submit", {}, lambda: "accepted")
    app.extend(core)
    assert app.names == ["submit", "read_file", "glob"]
    with pytest.raises(ValueError, match="duplicate"):
        app.extend(core)
    assert app.names == ["submit", "read_file", "glob"]
