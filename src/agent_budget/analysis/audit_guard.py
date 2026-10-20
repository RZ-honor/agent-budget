# -*- coding: utf-8 -*-
"""汇总护栏臂在真实 SWE 任务上的触发数据。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_budget.paths import ARTIFACTS, REPORTS

HERE = Path(__file__).resolve().parent
ROOT = ARTIFACTS / "v12_guard"


def main() -> int:
    rows = []
    for rep_dir in sorted(ROOT.glob("r*")):
        for summary_path in sorted(rep_dir.glob("guard/*/summary.json")):
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            guard = payload.get("guard") or {}
            rows.append(
                {
                    "rep": rep_dir.name,
                    "task": summary_path.parent.name,
                    "status": payload.get("status"),
                    "exit_status": (payload.get("agent_info") or {}).get("exit_status"),
                    "agent_calls": payload.get("agent_calls"),
                    "elapsed_s": payload.get("elapsed_s"),
                    "f2p_pass": payload.get("f2p_pass"),
                    "p2p_pass": payload.get("p2p_pass"),
                    "p2p_failed": (payload.get("p2p") or {}).get("failed_ids"),
                    "success": payload.get("success"),
                    "history_offloaded": payload.get("history_offloaded"),
                    "prompt_chars_total": payload.get("prompt_chars_total"),
                    "guard": guard,
                }
            )
    totals = {}
    for key in (
        "actions", "executed", "signature_intercepts", "observation_folds",
        "folded_chars", "breaker_blocks", "breaker_trips", "forced_answers",
        "duplicate_commands",
    ):
        totals[key] = sum((r["guard"] or {}).get(key, 0) for r in rows)
    report = {"runs": rows, "totals": totals}
    dest = REPORTS / "guard_summary.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{'rep':>4} {'task':<13} {'exit':<15} {'calls':>5} {'elapsed':>8} f2p p2p succ | "
          f"act exec sig fold block trip force")
    for r in rows:
        g = r["guard"] or {}
        print(f"{r['rep']:>4} {r['task']:<13} {str(r['exit_status']):<15} {r['agent_calls']:>5} "
              f"{r['elapsed_s']:>8} {str(r['f2p_pass'])[0]}   {str(r['p2p_pass'])[0]}   "
              f"{str(r['success'])[0]}    | {g.get('actions',0):>3} {g.get('executed',0):>4} "
              f"{g.get('signature_intercepts',0):>3} {g.get('observation_folds',0):>4} "
              f"{g.get('breaker_blocks',0):>5} {g.get('breaker_trips',0):>4} {g.get('forced_answers',0):>5}")
    print("\n合计:", json.dumps(totals, ensure_ascii=False))
    print(f"written: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())