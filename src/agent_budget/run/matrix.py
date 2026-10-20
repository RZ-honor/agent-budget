# -*- coding: utf-8 -*-
"""交错执行 A/B 实验矩阵：消除「baseline 全部先跑」的执行顺序混杂。

每个重复轮次使用独立的 run-root 目录（runs_v3/r1、runs_v3/r2 ...），
因为 run_arm.py 会重建 workdir，同一目录跑第二次会覆盖上一次的结果。

轮内顺序：奇数轮 baseline→budget，偶数轮 budget→baseline（进一步抵消顺序效应）。

用法：
  python run_matrix.py --tasks pytest-11143,pytest-11148 --reps 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agent_budget._bootstrap  # noqa: F401,E402

HERE = Path(__file__).resolve().parent          # experiments/mini_swe_budget
REPO = HERE.parents[5]           # agenitic_mem 仓库根
PROJECT = HERE.parents[3]        # projects/agent-budget                           # agenitic_mem 仓库根
PYTHON = sys.executable

from agentops.session_eval import swe_task_setup as prep  # noqa: E402


def task_ready(task: str) -> bool:
    """预检任务是否已完成 F2P/P2P 节点解析，避免白跑一整轮 agent 后才失败。"""
    f2p = prep.WORK / task / "f2p_resolved.txt"
    p2p = prep.WORK / task / "p2p_gate.txt"
    return f2p.exists() and f2p.stat().st_size > 0 and p2p.exists() and p2p.stat().st_size > 0


def task_smoke_ok(task: str, smoke_timeout: int) -> bool:
    """环境冒烟：在模板仓库限时运行全部 F2P/P2P 节点，只验证能跑完、不验证通过。

    pytest 自身测试套件在 Windows 上有 spawn 子进程类测试，个别任务整体不可完成；
    准入阶段就识别并剔除，避免消耗数千秒运行预算后才以 timeout 收场。
    """
    repo = prep.WORK / task
    nodes: list[str] = []
    for name in ("f2p_resolved.txt", "p2p_gate.txt"):
        node_file = repo / name
        if node_file.exists():
            nodes += [line.strip() for line in node_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not nodes:
        return False
    try:
        # 模板仓库未打 agent 补丁，F2P 失败属预期；退出码 0/1 都算"能跑完"
        subprocess.run(
            [prep.venv_py(repo), "-m", "pytest", "-q", "-p", "no:cacheprovide", *nodes],
            cwd=str(repo),
            env={**os.environ, "PYTHONPATH": str(repo)},
            timeout=smoke_timeout,
            capture_output=True,
        )
        return True
    except subprocess.TimeoutExpired:
        return False


def run_one(
    arm: str,
    task: str,
    rep_root: Path,
    timeout_sec: int,
    obs_threshold: int,
    budget_threshold: int,
    budget_head_ratio: float,
    steps: int,
    history_budget: int,
    cmd_timeout: int,
    force_answer_after: int = 50,
    breaker_threshold: int = 3,
    breaker_cooldown: int = 300,
) -> dict:
    """调用已有的 run_arm.py 跑单次实验，返回该次的简要结果。"""
    cmd = [
        PYTHON,
        "-m", "agent_budget.run.arm",
        "--arm", arm,
        "--task", task,
        "--run-root", str(rep_root),
        "--steps", str(steps),
        "--obs-threshold", str(obs_threshold),
        "--budget-threshold", str(budget_threshold),
        "--budget-head-ratio", str(budget_head_ratio),
        "--history-budget", str(history_budget),
        "--cmd-timeout", str(cmd_timeout),
        "--force-answer-after", str(force_answer_after),
        "--breaker-threshold", str(breaker_threshold),
        "--breaker-cooldown", str(breaker_cooldown),
    ]
    started = time.time()
    record: dict = {"arm": arm, "task": task}
    # 单次超时必须捕获后继续，否则整个矩阵中断（pytest-11148 这类难任务会跑满步数上限）
    try:
        env = {**os.environ, "PYTHONPATH": str(PROJECT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
        proc = subprocess.run(cmd, cwd=str(PROJECT), env=env, timeout=timeout_sec)
        record["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired:
        record["exit_code"] = -1
        record["status"] = "timeout"
    record["wall_s"] = round(time.time() - started, 1)

    # run_arm.py 会把结果写到 <rep_root>/<arm>/<task>/summary.json
    summary_path = rep_root / arm / task / "summary.json"
    if summary_path.exists() and "status" not in record:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        record |= {
            "status": payload.get("status"),
            "success": payload.get("success"),
            "agent_calls": payload.get("agent_calls"),
            "observation": payload.get("observation"),
            "guard": payload.get("guard"),
        }
    elif "status" not in record:
        record["status"] = "missing_summary"
    return record


def already_done(rep_root: Path, arm: str, task: str) -> bool:
    """断点续跑：已有成功结果的组合直接跳过，避免重复消耗 API 与时间。"""
    summary_path = rep_root / arm / task / "summary.json"
    if not summary_path.exists():
        return False
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return payload.get("status") == "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default="pytest-11143,pytest-11148",
                        help="逗号分隔的 SWE-bench 实例短名")
    parser.add_argument("--reps", type=int, default=3, help="每个任务每臂的重复次数")
    parser.add_argument("--run-root", type=Path, default=HERE / "runs_v3")
    parser.add_argument("--timeout", type=int, default=2400, help="单次实验超时秒数")
    parser.add_argument("--obs-threshold", type=int, default=10000, help="offload 臂截断阈值（字符）")
    parser.add_argument("--budget-threshold", type=int, default=10000, help="budget 臂截断阈值（字符）")
    parser.add_argument("--budget-head-ratio", type=float, default=0.5, help="budget 臂头段占比")
    parser.add_argument("--steps", type=int, default=30, help="单次运行的模型调用步数上限")
    parser.add_argument("--start-rep", type=int, default=1, help="起始重复编号，用于在已有目录后追加而不覆盖")
    parser.add_argument("--arms", default="", help="只跑指定臂（逗号分隔），留空则 baseline/offload 交替")
    parser.add_argument("--history-budget", type=int, default=30000, help="hist 臂的上下文累计预算（字符）")
    parser.add_argument("--jobs", type=int, default=1,
                        help="并发运行数；每个运行有独立工作目录，装置线程安全，仅缩短整体墙钟时间")
    parser.add_argument("--cmd-timeout", type=int, default=600,
                        help="透传给 run_arm 的单条命令超时（秒），与运行墙钟解耦")
    parser.add_argument("--smoke-timeout", type=int, default=300,
                        help="任务准入冒烟测试的限时（秒），超时视为环境不可完成任务")
    parser.add_argument("--force-answer-after", type=int, default=50,
                        help="guard 臂强制作答阈值（已执行的工具调用次数）")
    parser.add_argument("--breaker-threshold", type=int, default=3,
                        help="guard 臂命令族连续工具层失败熔断阈值")
    parser.add_argument("--breaker-cooldown", type=int, default=300,
                        help="guard 臂熔断冷却秒数")
    args = parser.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    ready: list[str] = []
    for t in tasks:
        if not task_ready(t):
            print(f"[跳过任务] {t} 缺少 F2P/P2P 节点文件，未完成测试解析", flush=True)
            continue
        # 冒烟顺序执行即可：每个任务限时几分钟，挂死任务在这里就被剔除
        if not task_smoke_ok(t, args.smoke_timeout):
            print(f"[跳过任务] {t} 冒烟测试 {args.smoke_timeout}s 内未跑完 F2P/P2P，Windows 环境不可完成", flush=True)
            continue
        ready.append(t)
    tasks = ready
    if not tasks:
        print("没有可运行的任务")
        return 1
    matrix_log: list[dict] = []

    # 指定臂时按给定顺序执行，否则按轮次奇偶交替以抵消顺序效应
    fixed_arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    # 先按“轮次→任务→臂”排成队列，保持交错顺序；并发只是同时消费这个队列。
    # 单个运行的目录、虚拟环境、工作区都是独立的，所以并发不会改变每次实验的条件，
    # 只压缩整体墙钟时间。
    schedule: list[tuple[int, str, str]] = []
    for rep in range(args.start_rep, args.start_rep + args.reps):
        arms = fixed_arms or (["baseline", "offload"] if rep % 2 == 1 else ["offload", "baseline"])
        # 指定臂时也按轮次反转顺序，避免“某一臂总在同一时间窗先跑”的顺序效应
        if fixed_arms and rep % 2 == 0:
            arms = list(reversed(arms))
        for task in tasks:
            for arm in arms:
                schedule.append((rep, arm, task))

    log_path = args.run_root / "matrix_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()

    def execute(item: tuple[int, str, str]) -> dict | None:
        rep, arm, task = item
        rep_root = args.run_root / f"r{rep}"
        # 断点续跑：已成功的组合直接复用，不再重复调用模型
        if already_done(rep_root, arm, task):
            print(f"[跳过] r{rep} {arm} {task} 已有成功结果", flush=True)
            return None
        record = run_one(
            arm, task, rep_root, args.timeout,
            args.obs_threshold, args.budget_threshold, args.budget_head_ratio,
            args.steps,
            args.history_budget,
            args.cmd_timeout,
            args.force_answer_after,
            args.breaker_threshold,
            args.breaker_cooldown,
        )
        record |= {
            "rep": rep,
            "obs_threshold": args.obs_threshold,
            "budget_threshold": args.budget_threshold,
            "history_budget": args.history_budget,
            "steps": args.steps,
        }
        # 每完成一次立即落盘，避免中途中断丢失进度；并发下需要加锁保证行不交错
        with write_lock:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for record in pool.map(execute, schedule):
            if record:
                matrix_log.append(record)

    print(f"\n矩阵执行完成，共 {len(matrix_log)} 次运行，日志：{args.run_root / 'matrix_log.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
