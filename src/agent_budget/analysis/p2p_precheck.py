# -*- coding: utf-8 -*-
"""在未打补丁的干净克隆上重跑 F2P/P2P，得到「环境自身的失败面」。

为什么需要这一步：
  p2p_gate.txt 是在建任务时一次性筛出来的（当日该项通过才进闸门），
  但 Windows 上部分测试依赖用户目录/环境残留，隔几天可能变红。
  若不做前置快照，就会把环境自身的红灯记成 agent 的回归。

判定口径随之变为 SWE-bench 的标准语义：
  success = F2P 全绿 且 (post_p2p_failed - pre_p2p_failed) 为空，
  即「没有把原本绿的测试弄红」，而不是「闸门 63 项一个都不许红」。

用法: python p2p_precheck.py pylint-8281 pytest-11143 pytest-9359
输出: p2p_precheck_<date>.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from agent_budget.paths import ARTIFACTS, REPORTS

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[5]

from agentops.session_eval import stage1_task_swe as dsh_runner  # noqa: E402
from agentops.session_eval import swe_task_setup as prep  # noqa: E402

WORK_ROOT = REPO / ".tmp" / "p2p_pre"


def setup_local(task: str, rundir: Path) -> dict:
    """复刻 swe_task_setup.setup 的三步（回到 base_commit → 补生成文件 → 打 test_patch）。

    不用 git clone：沙箱内本地 clone 会让 git 去起 sh，sh 建信号管道被拒（Win32 error 5）。
    改为整目录复制（排除 .venv，venv 走 _venv_from.txt 共享），再显式 checkout + apply，
    得到与 clone 完全一致的「base_commit + test_patch、无 agent 改动」状态。
    """
    tmpl = prep.WORK / task
    meta = json.loads((prep.WORK / "task_meta.json").read_text(encoding="utf-8"))[task]
    prep.remove_tree(rundir)
    rundir.parent.mkdir(parents=True, exist_ok=True)
    copy = subprocess.run(
        ["robocopy", str(tmpl), str(rundir), "/E", "/XD", ".venv", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1"],
        capture_output=True,
    )
    if copy.returncode >= 8:
        raise SystemExit(f"robocopy failed for {task}: {copy.returncode}")
    subprocess.run(["git", "-C", str(rundir), "checkout", "--quiet", "--", "."], check=True)
    for rel in prep.GENERATED:
        src, dst = tmpl / rel, rundir / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    (rundir / "test_patch.diff").write_text(meta["test_patch"], encoding="utf-8")
    r = subprocess.run(["git", "-C", str(rundir), "apply", "test_patch.diff"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"test_patch failed for {task}: {r.stderr}")
    py = prep.venv_py(tmpl)
    pypath = str(rundir / "src") if (rundir / "src").is_dir() else str(rundir)
    env = {
        "short": task, "instance_id": meta["instance_id"], "base_commit": meta["base_commit"],
        "python": py, "pythonpath": pypath,
        "f2p": str(tmpl / "f2p_resolved.txt"), "p2p_gate": str(tmpl / "p2p_gate.txt"),
    }
    return env


def precheck(task: str) -> dict:
    work = WORK_ROOT / task / "work"
    started = time.time()
    env = setup_local(task, work)
    f2p_nodes = dsh_runner.read_nodes(Path(env["f2p"]))
    p2p_nodes = dsh_runner.read_nodes(Path(env["p2p_gate"]))
    pre_f2p = dsh_runner.run_nodes(env, work, f2p_nodes)
    pre_p2p = dsh_runner.run_nodes(env, work, p2p_nodes)
    return {
        "task": task,
        "instance_id": env["instance_id"],
        "base_commit": env["base_commit"],
        "f2p_n": len(f2p_nodes),
        "p2p_gate_n": len(p2p_nodes),
        # 干净树上 F2P 应当全红（补丁没打），这是任务有效性的前提
        "f2p_pre": pre_f2p,
        # 干净树上 P2P 闸门里仍然红的节点 = 环境自身失败面
        "p2p_pre": pre_p2p,
        "p2p_pre_failed_ids": pre_p2p.get("failed_ids", []),
        "elapsed_s": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="在未打补丁的干净克隆上重跑 F2P/P2P，测出环境自身的失败面。")
    parser.add_argument("tasks", nargs="*", default=None,
                        help="任务名，可多个；缺省跑 pylint-8281 / pytest-11143 / pytest-9359")
    args = parser.parse_args()
    tasks = args.tasks or ["pylint-8281", "pytest-11143", "pytest-9359"]
    out = []
    for task in tasks:
        rep = precheck(task)
        out.append(rep)
        print(
            f"{task}: f2p_pre_failed={rep['f2p_pre']['failed']}/{rep['f2p_n']} "
            f"p2p_pre_failed={rep['p2p_pre']['failed']}/{rep['p2p_gate_n']} "
            f"ids={rep['p2p_pre_failed_ids']}",
            flush=True,
        )
    path = REPORTS / "p2p_precheck.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
