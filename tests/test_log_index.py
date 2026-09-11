import json

from agentloop.log_index import index_log_text


def test_long_bodies_keep_signal_positions_without_returning_whole_lines():
    rows = []
    for index, tail in enumerate(
        [
            "request_timeout=10000 timeout 504",
            "409 already processing",
            "Cost:20147ms completed success",
        ]
    ):
        rows.append(
            f"1 Info 2026-09-08T10:02:0{index}+08:00 file.go:1 "
            "example.trade.order _msg=CreateOrderInOneStep "
            + "x" * 50000
            + " "
            + tail
            + "\n"
        )
    text = "".join(rows)
    result = index_log_text(text, "CreateOrderInOneStep")
    assert result["matched_lines"] == 3
    assert len(json.dumps(result)) < 6000
    assert result["events"][0]["time"] < result["events"][-1]["time"]
    rendered = json.dumps(result)
    for token in ["10000", "504", "409", "20147"]:
        assert token in rendered
    for event in result["events"]:
        for snippet in event["snippets"]:
            start = snippet["char_offset"]
            assert (
                rows[event["line"] - 1][start : start + len(snippet["text"])]
                == snippet["text"]
            )


def test_omissions_are_explicit_and_labels_do_not_claim_root_cause():
    result = index_log_text(
        "\n".join(f"unknown format timeout {i}" for i in range(100)), max_events=1
    )
    assert result["matched_lines"] == 100
    assert result["selected_events"] == 1 and result["omitted_events"] == 99
    assert result["events"][0]["time"] is None
    assert "not a causal conclusion" in result["scope"]
