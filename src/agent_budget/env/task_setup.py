# -*- coding: utf-8 -*-
"""复刻 swe_task_setup.setup，但不走 git 的本地传输。

为什么不能直接用 prep.setup：
  沙箱内 `git clone <本地路径>` 会让 git 经由 sh 去起 upload-pack，
  而 sh 建信号管道被拒（`couldn't create signal pipe, Win32 error 5`），clone 必失败。
  换 file:// 也一样，问题在 sh 而不在路径写法。

等价做法：
  整目录复制（排除 .venv，venv 由 _venv_from.txt 指向共享环境）→ git clean -xdf 去掉
  模板里未跟踪的残留（含 f2p/p2p 节点文件，不能带进工作区）→ checkout 回到 base_commit
  → 补回 gitignored 的生成文件 → 打 test_patch。
  结果与 clone 完全一致：真实 .git 历史、base_commit、test_patch 已打、无 agent 改动。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 依赖路径统一由 _bootstrap 负责（幂等）
import agent_budget._bootstrap  # noqa: F401,E402

from agentops.session_eval import swe_task_setup as prep  # noqa: E402


def setup(task: str, rundir: Path) -> dict:
    tmpl = prep.WORK / task
    # 必须绝对化：pythonpath 会经 PYTHONPATH 传进 Git Bash 子进程，相对路径会在 work/ 下解析失败
    rundir = Path(rundir).resolve()
    meta = json.loads((prep.WORK / "task_meta.json").read_text(encoding="utf-8"))[task]
    prep.remove_tree(rundir)
    rundir.parent.mkdir(parents=True, exist_ok=True)
    copied = subprocess.run(
        ["robocopy", str(tmpl), str(rundir), "/E", "/XD", ".venv",
         "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1"],
        capture_output=True,
    )
    if copied.returncode >= 8:
        raise SystemExit(f"robocopy failed for {task}: {copied.returncode}")
    # 模板工作区带着上一步的未跟踪文件（节点清单、report 等），先清掉再回滚
    subprocess.run(["git", "-C", str(rundir), "clean", "-xdfq"], capture_output=True)
    subprocess.run(["git", "-C", str(rundir), "checkout", "--quiet", "--", "."], check=True)
    for rel in prep.GENERATED:
        src, dst = tmpl / rel, rundir / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (rundir / "test_patch.diff").write_text(meta["test_patch"], encoding="utf-8")
    applied = subprocess.run(
        ["git", "-C", str(rundir), "apply", "test_patch.diff"], capture_output=True, text=True
    )
    if applied.returncode != 0:
        raise SystemExit(f"test_patch failed for {task}: {applied.stderr}")
    # 交付给 agent 的工作区必须是「纯 issue、无测试」的原协议状态：
    # 上面打的那份 test_patch 只用于确认环境可判定，这里撤回并删掉补丁文件，
    # 否则 agent 能直接读到 F2P 测试的断言与期望值，成功率的含义就不成立了。
    # 判定时 run_arm.restore_test_patch 会重新打回，判分不受影响。
    for rel in ("test_patch.diff",):
        (rundir / rel).unlink(missing_ok=True)
    for rel in re.findall(r"^diff --git a/(\S+) b/", meta["test_patch"], re.M):
        subprocess.run(["git", "-C", str(rundir), "checkout", "--", rel], capture_output=True)
    python = prep.venv_py(tmpl)
    pythonpath = str(rundir / "src") if (rundir / "src").is_dir() else str(rundir)
    env = {
        "short": task,
        "instance_id": meta["instance_id"],
        "base_commit": meta["base_commit"],
        "python": python,
        "pythonpath": pythonpath,
        "f2p": str(tmpl / "f2p_resolved.txt"),
        "p2p_gate": str(tmpl / "p2p_gate.txt"),
    }
    (rundir / "_env.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
    return env