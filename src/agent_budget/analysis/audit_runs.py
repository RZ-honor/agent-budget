# -*- coding: utf-8 -*-
"""汇总全部真实运行，按两个口径分别判定。

口径 A（表格里的 success 字段）：F2P 全绿 且 闸门 P2P 63/63 全绿。
  pylint-8281 的闸门里有一个测试在干净树上就红（见 reports/p2p_precheck.json），
  该口径会把环境自身的红灯记成 agent 回归，因此不作为最终结论。

口径 B（SWE-bench 标准语义）：
  success = F2P 全绿 且 （post 失败的 P2P 节点 ⊆ 干净树上已失败的 P2P 节点）
  即「没有把原本能过的测试弄红」。

只统计真正产出 summary.json 的运行；timeout / missing_summary 单独列出，不混进分母。
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_budget.paths import ARTIFACTS, REPORTS

HERE = Path(__file__).resolve().parent
PRE = REPORTS / "p2p_precheck.json"

# 闸门里必须在判定时剔除的节点：与本机权限绑定，和 agent 改动无关。
# test_warn_about_old_home 会往当前用户的本地配置目录写一个「十年前日期」的抑制文件，
# 该目录在本机不可写（干净树上同样是 PermissionError: WinError 5），
# 因此它在任何补丁下都不可能变绿；同一节点在 r1/r2 的 baseline 与 hist 四次运行里全部报红，
# 且四次补丁互不相同 —— 足以排除「被某个补丁弄红」。
ENV_EXCLUDED = {
    "pylint-8281": {"tests/lint/unittest_lint.py::test_warn_about_old_home"},
}

# 供参考：干净树上的失败面。注意这份前置快照是在受限沙箱里跑的，
# 需要 multiprocessing.Pipe 的节点（test_custom_should_analyze_file、test_multiprocessing[2]）
# 在沙箱里一律 WinError 5，属于测量环境伪影，不能当成任务自身的红灯，故不参与判定。


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect() -> tuple[list[dict], list[dict]]:
    pre = {item["task"]: item for item in json.loads(PRE.read_text(encoding="utf-8"))}
    ok, bad = [], []
    roots = [(ARTIFACTS / "v10" / f"r{rep}") for rep in (1, 2)]
    roots += [(ARTIFACTS / "v11_9359" / f"r{rep}") for rep in (1, 2)]
    for rep_root in roots:
        if not rep_root.is_dir():
            continue
        for arm_dir in sorted(rep_root.iterdir()):
            if not arm_dir.is_dir():
                continue
            for task_dir in sorted(arm_dir.iterdir()):
                if not task_dir.is_dir():
                    continue
                summary = load(task_dir / "summary.json")
                record = {
                    "root": rep_root.parent.name,
                    "rep": rep_root.name,
                    "arm": arm_dir.name,
                    "task": task_dir.name,
                }
                if summary is None:
                    record["status"] = "no_summary"
                    bad.append(record)
                    continue
                pre_task = pre.get(task_dir.name, {})
                pre_failed = set(pre_task.get("p2p_pre_failed_ids", []))
                post_failed = set((summary.get("p2p") or {}).get("failed_ids", []))
                excluded = ENV_EXCLUDED.get(task_dir.name, set())
                record |= {
                    "status": summary.get("status"),
                    "exit_status": (summary.get("agent_info") or {}).get("exit_status"),
                    "agent_calls": summary.get("agent_calls"),
                    "elapsed_s": summary.get("elapsed_s"),
                    "f2p_pass": summary.get("f2p_pass"),
                    "p2p_pass_strict": summary.get("p2p_pass"),
                    "post_p2p_failures": sorted(post_failed),
                    "env_excluded_failures": sorted(post_failed & excluded),
                    "new_p2p_failures": sorted(post_failed - pre_failed - excluded),
                    "pre_p2p_failures": sorted(pre_failed),
                    "success_strict": summary.get("success"),
                    "observation": summary.get("observation"),
                    "prompt_chars_total": summary.get("prompt_chars_total"),
                    "history_offloaded": summary.get("history_offloaded"),
                    "obs_read_commands": (summary.get("commands") or {}).get("obs_read_commands"),
                    "commands": summary.get("commands"),
                    "diff_chars": summary.get("diff_chars"),
                }
                record["success_standard"] = bool(record["f2p_pass"]) and not record["new_p2p_failures"]
                ok.append(record)
    return ok, bad


def aggregate(runs: list[dict]) -> dict:
    out = {}
    for arm in ("baseline", "hist"):
        arm_runs = [r for r in runs if r["arm"] == arm]
        obs = [r["observation"] for r in arm_runs if r.get("observation")]
        out[arm] = {
            "runs": len(arm_runs),
            "success_strict": sum(1 for r in arm_runs if r.get("success_strict")),
            "success_standard": sum(1 for r in arm_runs if r.get("success_standard")),
            "submitted": sum(1 for r in arm_runs if r.get("exit_status") == "Submitted"),
            "limit_exceeded": sum(1 for r in arm_runs if r.get("exit_status") == "LimitsExceeded"),
            "raw_chars": sum(o["raw_chars"] for o in obs),
            "visible_chars": sum(o["visible_chars"] for o in obs),
            "visible_payload_chars": sum(o["visible_payload_chars"] for o in obs),
            "elided_chars": sum(o["elided_chars"] for o in obs),
            "truncated_messages": sum(o["truncated_messages"] for o in obs),
            "observation_messages": sum(o["observation_messages"] for o in obs),
            "prompt_chars_total": sum(r.get("prompt_chars_total") or 0 for r in arm_runs),
            "history_offloaded": sum(r.get("history_offloaded") or 0 for r in arm_runs),
            "obs_read_commands": sum(r.get("obs_read_commands") or 0 for r in arm_runs),
            "commands": sum((r.get("commands") or {}).get("commands", 0) for r in arm_runs),
            "repeated_commands": sum((r.get("commands") or {}).get("repeated_commands", 0) for r in arm_runs),
        }
    return out


def pairs(runs: list[dict]) -> list[dict]:
    index = {(r["root"], r["rep"], r["task"], r["arm"]): r for r in runs}
    out = []
    for (root, rep, task, arm) in list(index):
        if arm != "baseline":
            continue
        base = index[(root, rep, task, "baseline")]
        hist = index.get((root, rep, task, "hist"))
        if not hist:
            continue
        b, h = base.get("prompt_chars_total") or 0, hist.get("prompt_chars_total") or 0
        out.append({
            "root": root, "rep": rep, "task": task,
            "baseline_chars": b, "hist_chars": h,
            "reduction_pct": round((1 - h / b) * 100, 1) if b else None,
            "success_baseline": base.get("success_standard"),
            "success_hist": hist.get("success_standard"),
            "exit_baseline": base.get("exit_status"),
            "exit_hist": hist.get("exit_status"),
        })
    out.sort(key=lambda r: (r["root"], r["task"], r["rep"]))
    return out


def main() -> int:
    runs, missing = collect()
    report = {
        "runs": runs,
        "missing_summary": missing,
        "aggregate": aggregate(runs),
        "pairs": pairs(runs),
    }
    dest = REPORTS / "summary_replication.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("=== 每次运行 ===")
    for r in runs:
        print(f"{r['root']:>9} {r['rep']} {r['arm']:<8} {r['task']:<13} exit={r['exit_status']!s:<15} "
              f"f2p={r['f2p_pass']} strict={r['success_strict']} std={r['success_standard']} "
              f"new_p2p_fail={len(r['new_p2p_failures'])} prompt_chars={r['prompt_chars_total']} "
              f"offload={r['history_offloaded']} obs_read={r['obs_read_commands']}")
    print("\n=== 缺失 summary（未完成） ===")
    for r in missing:
        print(f"{r['root']:>9} {r['rep']} {r['arm']:<8} {r['task']}")
    print("\n=== 逐臂汇总 ===")
    for arm, a in report["aggregate"].items():
        print(f"{arm}: runs={a['runs']} strict={a['success_strict']}/{a['runs']} "
              f"std={a['success_standard']}/{a['runs']} submitted={a['submitted']} "
              f"limit={a['limit_exceeded']} obs_raw={a['raw_chars']} obs_visible={a['visible_chars']} "
              f"trunc={a['truncated_messages']} elided={a['elided_chars']} "
              f"prompt_chars={a['prompt_chars_total']} offload={a['history_offloaded']} "
              f"obs_read={a['obs_read_commands']} repeat={a['repeated_commands']}/{a['commands']}")
    print("\n=== 同任务配对 ===")
    for p in report["pairs"]:
        print(f"{p['task']:<13} {p['rep']} {p['baseline_chars']:>9} -> {p['hist_chars']:>9} "
              f"{p['reduction_pct']}%  success {p['success_baseline']}->{p['success_hist']} "
              f"exit {p['exit_baseline']}->{p['exit_hist']}")
    print(f"\nwritten: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
