import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_budget.agents.observation import render_observation


def test_short_output_is_identical_in_both_arms():
    output = "small output\n"

    assert render_observation("baseline", output) == render_observation("budget", output)


def test_budget_arm_truncates_long_output_with_explicit_marker():
    output = "H" * 6000 + "M" * 8000 + "T" * 6000

    baseline = render_observation("baseline", output)
    budget = render_observation("budget", output)

    assert len(baseline) > len(output)
    assert "M" * 1000 in baseline
    assert "M" * 1000 not in budget
    assert "H" * 4000 in budget
    assert "T" * 4000 in budget
    assert "10000 characters elided" in budget
