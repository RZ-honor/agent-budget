import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_budget.env.git_bash import GitBashEnvironment
from minisweagent.exceptions import Submitted


def test_executes_command_through_git_bash():
    env = GitBashEnvironment(cwd=str(ROOT), timeout=30)
    result = env.execute({"command": "printf 'hello-from-bash'"})

    assert result["returncode"] == 0
    assert result["output"] == "hello-from-bash"
    assert result["exception_info"] == ""


def test_detects_submission_marker():
    env = GitBashEnvironment(cwd=str(ROOT), timeout=30)

    with pytest.raises(Submitted) as exc:
        env.execute({"command": "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nfinal patch\\n'"})

    assert exc.value.messages[0]["extra"]["submission"] == "final patch\n"
