# -*- coding: utf-8 -*-
"""运行时护栏核心：收敛三层 + 按命令族的失败熔断。

这个模块不依赖 minisweagent，只做判定，便于单测；真正的挂钩在 guarded_agent.py。

三层收敛
--------
1. 签名拦截：与**上一条**完全相同的命令直接拦下，不再执行。
   只拦连续重复是刻意的：SWE 任务里改完代码重跑同一条测试是合法行为，
   非连续的同签名调用一律放行，否则护栏本身就会破坏任务。
2. 观测折叠：不同命令返回了逐字相同的内容时，只留一句指针，不再整段重复入模。
3. 强制作答：工具调用累计到阈值后停用工具描述，模型只能输出文本，随即结束本轮。

失败熔断
--------
按**命令族**（命令首个词，如 pytest、git、grep）统计失败，连续 3 次进入冷却窗口。
只把工具层失败计入（命令不存在、权限拒绝、执行异常），
测试断言失败不算——那不是工具坏了，把它算进去会在正常调试时误熔断。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

# 工具层失败的返回码：126 不可执行、127 命令不存在、-1 执行异常
TOOL_FAILURE_CODES = {-1, 126, 127}
TOOL_FAILURE_MARKERS = (
    "command not found",
    "permission denied",
    "no such file or directory",
    "cannot execute",
    "fatal error",
    "couldn't create signal pipe",
)
# 测试框架的汇总行：带这个的行说明命令确实跑起来了，是非零返回里的「断言失败」，
# 属于正常调试结果，不应计入工具层失败，否则正常迭代三步就会误熔断。
TEST_SUMMARY = re.compile(r"\b\d+\s+(passed|failed|error|errors|skipped)\b", re.IGNORECASE)


def command_family(command: str) -> str:
    """取命令首个词作为族名，跳过 VAR=value 前缀与常见包裹词。"""
    text = command.strip()
    text = re.sub(r"^(\w+=\S*\s+)+", "", text)
    text = re.sub(r"^(sudo|env|time|nohup)\s+", "", text)
    match = re.match(r"[A-Za-z0-9_./-]+", text)
    return match.group(0).rsplit("/", 1)[-1] if match else "unknown"


def is_tool_failure(returncode: int, output: str) -> bool:
    """判定一次执行是否属于「工具层失败」。

    这是熔断的唯一入口，判错会导致两种后果：
      漏判 —— 环境坏掉时白烧几十步（2026-11-08 23:47 那次 60 步全废就是这种）；
      误判 —— 把正常的测试断言失败当成工具故障，连熔三族，任务直接废掉。
    因此除了明确返回码与关键词，其余非零返回要看有没有测试汇总行：
    有汇总行 = 命令跑通了、是断言红，不算工具失败；没有 = 执行本身出了问题，算。
    """
    if returncode in TOOL_FAILURE_CODES:
        return True
    if returncode == 0:
        return False
    lowered = (output or "").lower()
    if any(marker in lowered for marker in TOOL_FAILURE_MARKERS):
        return True
    return not TEST_SUMMARY.search(output or "")


def normalize(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip()


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    trips: int = 0


@dataclass
class GuardStats:
    actions: int = 0
    executed: int = 0
    signature_intercepts: int = 0
    observation_folds: int = 0
    folded_chars: int = 0
    breaker_blocks: int = 0
    breaker_trips: int = 0
    forced_answers: int = 0
    duplicate_commands: int = 0  # 全局同签名重复（含非连续，仅统计不拦截）


@dataclass
class Decision:
    kind: str          # execute | intercept | block
    reason: str = ""


@dataclass
class RuntimeGuard:
    force_answer_after: int = 0
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 300.0
    dedupe_observations: bool = True
    # 连续重复拦截默认关闭：21 次真实运行共 1144 条命令，连续完全重复 0 次，
    # 打开也永不命中，反而有「拦掉合法重跑」的风险，因此只保留计数用于审计。
    intercept_repeats: bool = False
    stats: GuardStats = field(default_factory=GuardStats)
    _last_signature: str = ""
    _seen_signatures: set = field(default_factory=set)
    _seen_observations: dict = field(default_factory=dict)  # hash -> 首次序号
    _breakers: dict = field(default_factory=dict)
    _forced: bool = False

    # -- 失败熔断 ------------------------------------------------------
    def _breaker(self, family: str) -> BreakerState:
        return self._breakers.setdefault(family, BreakerState())

    def breaker_open(self, family: str, now: float | None = None) -> bool:
        state = self._breaker(family)
        if state.failures < self.breaker_threshold:
            return False
        now = time.time() if now is None else now
        if now - state.opened_at >= self.breaker_cooldown_s:
            state.failures = 0
            state.opened_at = 0.0
            return False
        return True

    # -- 执行前判定 ----------------------------------------------------
    def pre_action(self, command: str) -> Decision:
        signature = normalize(command)
        family = command_family(command)
        self.stats.actions += 1
        if signature in self._seen_signatures:
            self.stats.duplicate_commands += 1
        if self.intercept_repeats and signature and signature == self._last_signature:
            self.stats.signature_intercepts += 1
            return Decision(
                "intercept",
                "这一条命令与上一步完全相同，已拦截且未执行；"
                "如果确实需要重跑，请先说明上一步结果为何不可用，并换成不同的命令或不同的参数。",
            )
        if self.breaker_open(family):
            self.stats.breaker_blocks += 1
            remaining = self._breaker(family)
            return Decision(
                "block",
                f"命令族 {family} 连续 {remaining.failures} 次工具层失败，已进入冷却窗口，"
                "本次调用被拒绝；请改用其它命令，或先确认该命令是否存在于当前环境。",
            )
        self._last_signature = signature
        self._seen_signatures.add(signature)
        return Decision("execute")

    # -- 执行后记账 ----------------------------------------------------
    def post_action(self, command: str, output: str, returncode: int) -> tuple[str, bool]:
        self.stats.executed += 1
        family = command_family(command)
        state = self._breaker(family)
        if is_tool_failure(returncode, output):
            state.failures += 1
            if state.failures == self.breaker_threshold:
                state.opened_at = time.time()
                state.trips += 1
                self.stats.breaker_trips += 1
        else:
            state.failures = 0
            state.opened_at = 0.0
        if not self.dedupe_observations or not output.strip():
            return output, False
        digest = hashlib.sha1(output.encode("utf-8", "replace")).hexdigest()
        if digest in self._seen_observations:
            self.stats.observation_folds += 1
            self.stats.folded_chars += len(output)
            first = self._seen_observations[digest]
            return (
                f"[observation folded] 本次输出与第 {first} 次执行的输出逐字相同（{len(output)} 字符），"
                "已折叠不再重复入模；需要原文时按第 "
                f"{first} 次执行的命令重新取其中一段，避免整段重放。",
                True,
            )
        self._seen_observations[digest] = self.stats.executed
        return output, False

    # -- 强制作答 ------------------------------------------------------
    def should_force_answer(self, tool_calls_used: int) -> bool:
        if self._forced or self.force_answer_after <= 0:
            return False
        if tool_calls_used < self.force_answer_after:
            return False
        self._forced = True
        self.stats.forced_answers += 1
        return True

    def summary(self) -> dict:
        return {
            "force_answer_after": self.force_answer_after,
            "breaker_threshold": self.breaker_threshold,
            "breaker_cooldown_s": self.breaker_cooldown_s,
            **self.stats.__dict__,
        }