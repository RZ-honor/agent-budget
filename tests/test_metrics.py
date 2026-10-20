import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_budget.run.arm import observation_metrics


def test_observation_metrics_compare_raw_and_model_visible_chars():
    messages = [
        {
            "role": "assistant",
            "content": "thinking",
            "extra": {},
        },
        {
            "role": "tool",
            "content": "visible",
            "extra": {"raw_output": "x" * 20},
        },
        {
            "role": "tool",
            "content": "another",
            "extra": {"raw_output": "y" * 5},
        },
    ]

    metrics = observation_metrics(messages)

    assert metrics["observation_messages"] == 2
    assert metrics["raw_chars"] == 25
    assert metrics["visible_chars"] == len("visible") + len("another")
    assert metrics["visible_payload_chars"] == 25
    assert metrics["elided_chars"] == 0


def test_truncated_observation_payload_is_capped_at_10000():
    messages = [
        {
            "role": "tool",
            "content": "head\n10000 characters elided\ntail",
            "extra": {"raw_output": "x" * 25000},
        }
    ]

    metrics = observation_metrics(messages)

    assert metrics["raw_chars"] == 25000
    # 截断后模型看到的是「head + 省略标记 + tail」，只有这些字符进入上下文，
    # 因此可见 payload 按实际可见长度计（不是被省略掉的那一万字符）。
    assert metrics["visible_payload_chars"] == len("head\n10000 characters elided\ntail")
    assert metrics["truncated_messages"] == 1
