# -*- coding: utf-8 -*-
"""护栏核心的单元测试：只验判定，不碰模型与真实命令。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_budget.guard import RuntimeGuard, command_family, is_tool_failure


def make_guard(**kwargs) -> RuntimeGuard:
    config = {"force_answer_after": 0, "breaker_threshold": 3, "breaker_cooldown_s": 300.0}
    return RuntimeGuard(**(config | kwargs))


def test_command_family_strips_env_prefix_and_path():
    assert command_family("PYTHONPATH=src python -m pytest -q") == "python"
    assert command_family("sudo /usr/bin/git status") == "git"
    assert command_family("   ") == "unknown"


def test_tool_failure_excludes_test_assertion_failures():
    assert is_tool_failure(127, "") is True
    assert is_tool_failure(1, "1 failed, 2 passed") is False
    assert is_tool_failure(1, "bash: foo: command not found") is True
    # 环境级崩溃（昨日 bash 建管道被拒）也要算工具失败，否则熔断形同虚设
    assert is_tool_failure(-1073741502, "bash.exe: fatal error - couldn't create signal pipe") is True
    # 非零返回但既无测试汇总也无故障关键词，视为执行异常
    assert is_tool_failure(2, "Traceback (most recent call last):") is True
    assert is_tool_failure(0, "63 passed in 5.04s") is False


def test_consecutive_duplicate_interception_is_off_by_default():
    guard = make_guard()
    assert guard.pre_action("pytest -q testing/code").kind == "execute"
    assert guard.pre_action("pytest -q testing/code").kind == "execute"
    assert guard.stats.signature_intercepts == 0
    assert guard.stats.duplicate_commands == 1


def test_consecutive_duplicate_interception_when_enabled():
    guard = make_guard(intercept_repeats=True)
    assert guard.pre_action("pytest -q testing/code").kind == "execute"
    assert guard.pre_action("pytest -q testing/code").kind == "intercept"
    # 中间插入了别的命令后重跑同一条，属于合法重跑，必须放行
    assert guard.pre_action("ls").kind == "execute"
    assert guard.pre_action("pytest -q testing/code").kind == "execute"


def test_identical_observation_is_folded_once():
    guard = make_guard()
    first, folded = guard.post_action("git status", "nothing to commit", 0)
    assert folded is False and first == "nothing to commit"
    second, folded = guard.post_action("git diff", "nothing to commit", 0)
    assert folded is True
    assert "第 1 次执行" in second
    assert guard.stats.observation_folds == 1
    assert guard.stats.folded_chars == len("nothing to commit")


def test_breaker_ignores_test_assertion_failures():
    guard = make_guard()
    for name in ("pytest a", "pytest b", "pytest c", "pytest d"):
        assert guard.pre_action(name).kind == "execute"
        guard.post_action(name, "1 failed, 2 passed", 1)
    assert guard.stats.breaker_trips == 0


def test_breaker_trips_and_blocks_only_that_family():
    guard = make_guard()
    for name in ("make a", "make b", "make c"):
        assert guard.pre_action(name).kind == "execute"
        guard.post_action(name, "bash: make: command not found", 127)
    assert guard.stats.breaker_trips == 1
    decision = guard.pre_action("make d")
    assert decision.kind == "block" and "冷却" in decision.reason
    # 别的命令族不受牵连
    assert guard.pre_action("ls -la").kind == "execute"


def test_breaker_recovers_after_cooldown():
    guard = make_guard(breaker_cooldown_s=0.0)
    for name in ("make a", "make b", "make c"):
        guard.pre_action(name)
        guard.post_action(name, "command not found", 127)
    assert guard.pre_action("make d").kind == "execute"


def test_force_answer_fires_once_at_threshold():
    guard = make_guard(force_answer_after=5)
    assert guard.should_force_answer(4) is False
    assert guard.should_force_answer(5) is True
    assert guard.should_force_answer(9) is False
    assert guard.stats.forced_answers == 1


def test_summary_reports_all_counters():
    guard = make_guard(force_answer_after=3)
    guard.pre_action("ls")
    guard.pre_action("ls")
    guard.should_force_answer(3)
    summary = guard.summary()
    assert summary["force_answer_after"] == 3
    assert summary["duplicate_commands"] == 1
    assert summary["forced_answers"] == 1