"""请求预算测试。"""

import pytest

from agentloop.budget import RequestBudget, estimate_utf8_tokens


def test_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        RequestBudget(0, 1)
    with pytest.raises(ValueError):
        RequestBudget(100, -1)
    with pytest.raises(ValueError):
        RequestBudget(100, 90, 10)
    with pytest.raises(ValueError):
        RequestBudget(True, 1)
    with pytest.raises(TypeError):
        RequestBudget(100, 1, token_estimator="not callable")


def test_utf8_estimator_is_conservative_for_unicode():
    assert estimate_utf8_tokens("abc") == 3
    assert estimate_utf8_tokens("你") == 3
    assert estimate_utf8_tokens("你a") == 4


def test_estimate_counts_serialized_system_tools_and_messages():
    seen = []

    def counter(text):
        seen.append(text)
        return len(text)

    budget = RequestBudget(1000, 100, 10, token_estimator=counter)
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "read_file"}, {"name": "bash"}]

    estimate = budget.estimate(messages, system="rules", tools=tools)

    assert seen == [
        '{"system":"rules","tools":[{"name":"read_file"},{"name":"bash"}],'
        '"messages":[{"role":"user","content":"hello"}]}'
    ]
    # content + request + system + one message + two tool wrappers
    assert estimate == len(seen[0]) + 12 + 4 + 4 + 16


def test_available_budget_and_fit_include_output_reserve_and_margin():
    budget = RequestBudget(100, 20, 10, token_estimator=lambda _: 50)
    messages = [{"role": "user", "content": "q"}]

    # Input limit is 70. The request itself includes 12 + 4 wrapper tokens.
    assert budget.available_input_tokens(messages) == 4
    assert budget.fits(messages)

    too_small = RequestBudget(80, 20, 10, token_estimator=lambda _: 50)
    assert too_small.available_input_tokens(messages) == -16
    assert not too_small.fits(messages)


def test_rejects_invalid_custom_counter_result():
    budget = RequestBudget(100, 20, token_estimator=lambda _: -1)
    with pytest.raises(ValueError, match="non-negative integer"):
        budget.estimate([])
