# -*- coding: utf-8 -*-
"""汇总交错矩阵实验的 summary.json，输出 A/B 对照指标。

用法：
  python -m agent_budget.analysis.summarize --run-root artifacts/v13_strict
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from agent_budget.paths import ARTIFACTS

HERE = Path(__file__).resolve().parent


def load_runs(run_root: Path) -> list[dict]:
    """扫描 <run_root>/r*/<arm>/<task>/summary.json，补齐 rep 编号。"""
    runs: list[dict] = []
    for summary_path in sorted(run_root.glob("r*/**/summary.json")):
        # 路径形如 runs_v3/r1/baseline/pytest-11143/summary.json
        parts = summary_path.parts
        try:
            rep = int([p for p in parts if p.startswith("r") and p[1:].isdigit()][0][1:])
        except IndexError:
            rep = 0
        arm = summary_path.parent.parent.name
        task = summary_path.parent.name
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        observation = payload.get("observation") or {}
        commands = payload.get("commands") or {}
        # budget 臂的阈值只存在于 budget_threshold 字段，offload 臂用 obs_threshold；
        # 同一臂在不同阈值下是不同实验条件，按 "臂@阈值" 分组，避免混算
        if arm == "budget":
            threshold = payload.get("budget_threshold") or 0
        else:
            threshold = payload.get("obs_threshold") or 0
        arm_key = f"{arm}@{threshold // 1000}k" if arm in ("budget", "offload") and threshold else arm
        runs.append({
            "rep": rep,
            "arm": arm_key,
            "task": task,
            "success": bool(payload.get("success")),
            "f2p_pass": bool(payload.get("f2p_pass")),
            "p2p_pass": bool(payload.get("p2p_pass")),
            "agent_calls": payload.get("agent_calls") or 0,
            "elapsed_s": payload.get("elapsed_s") or 0,
            "raw_chars": observation.get("raw_chars") or 0,
            "visible_payload_chars": observation.get("visible_payload_chars") or 0,
            "truncated_messages": observation.get("truncated_messages") or 0,
            "offload_count": payload.get("offload_count") or 0,
            # 重复命令是“观测被截断后模型拿不到所需信息”的直接信号，比字符数更贴近机制效果
            "commands": commands.get("commands") or 0,
            "repeated_commands": commands.get("repeated_commands") or 0,
            "repeat_rate_pct": commands.get("repeat_rate_pct") or 0.0,
            "obs_read_commands": commands.get("obs_read_commands") or 0,
        })
    return runs


def summarize(runs: list[dict], reference: str = "baseline") -> dict:
    """按 臂 分组求均值，并给出各实验臂相对参考臂的变化。

    参考臂默认取 baseline；同阈值对照场景（budget@2k 对 offload@2k）没有
    baseline，由调用方传入 --reference 指定，默认退化为字典序靠前的臂。
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        groups[r["arm"]].append(r)

    result: dict[str, dict] = {}
    for arm, rows in groups.items():
        result[arm] = {
            "n": len(rows),
            "success_rate": round(sum(r["success"] for r in rows) / len(rows), 4),
            "f2p_pass_rate": round(sum(r["f2p_pass"] for r in rows) / len(rows), 4),
            "p2p_pass_rate": round(sum(r["p2p_pass"] for r in rows) / len(rows), 4),
            "mean_agent_calls": round(statistics.fmean(r["agent_calls"] for r in rows), 2),
            "mean_raw_chars": round(statistics.fmean(r["raw_chars"] for r in rows), 1),
            "mean_visible_payload_chars": round(statistics.fmean(r["visible_payload_chars"] for r in rows), 1),
            "mean_elapsed_s": round(statistics.fmean(r["elapsed_s"] for r in rows), 1),
            "mean_repeat_rate_pct": round(statistics.fmean(r["repeat_rate_pct"] for r in rows), 2),
            "mean_obs_read_commands": round(statistics.fmean(r["obs_read_commands"] for r in rows), 2),
            "total_truncated_messages": sum(r["truncated_messages"] for r in rows),
            "total_offload": sum(r["offload_count"] for r in rows),
            "total_commands": sum(r["commands"] for r in rows),
            "total_repeated_commands": sum(r["repeated_commands"] for r in rows),
        }

    # 参考臂不存在时退化为字典序第一个臂，保证新场景也能输出对比列
    if reference not in result:
        reference = sorted(result)[0]
    base = result[reference]
    deltas: dict[str, dict] = {}
    for arm, row in result.items():
        if arm == reference:
            continue

        def drop(key: str) -> float:
            b = base[key]
            return round((b - row[key]) / b * 100, 2) if b else 0.0

        deltas[arm] = {
            "success_rate_pp": round((row["success_rate"] - base["success_rate"]) * 100, 2),
            "mean_visible_payload_chars_drop_pct": drop("mean_visible_payload_chars"),
            "mean_agent_calls_drop_pct": drop("mean_agent_calls"),
            "mean_elapsed_s_drop_pct": drop("mean_elapsed_s"),
            "repeat_rate_diff_pp": round(row["mean_repeat_rate_pct"] - base["mean_repeat_rate_pct"], 2),
        }
    result["__deltas__"] = deltas
    result["__reference__"] = reference
    return result


def render_markdown(runs: list[dict], summary: dict) -> str:
    """生成 Markdown 报告：逐次原始数据 + 分组汇总。"""
    lines = ["# observation budget A/B 实验汇总", ""]

    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_task[r["task"]].append(r)

    lines += ["| 任务 | 臂 | 重复 | 成功 | F2P | P2P | 模型调用 | 可见观测字符 | 原始字符 | 截断次数 | 重复命令率 | 回读次数 | 耗时(s) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for task, rows in sorted(by_task.items()):
        for r in sorted(rows, key=lambda x: (x["arm"], x["rep"])):
            lines.append(
                f"| {task} | {r['arm']} | r{r['rep']} | {'是' if r['success'] else '否'} | "
                f"{'通过' if r['f2p_pass'] else '失败'} | {'通过' if r['p2p_pass'] else '失败'} | "
                f"{r['agent_calls']} | {r['visible_payload_chars']} | {r['raw_chars']} | "
                f"{r['truncated_messages']} | {r['repeat_rate_pct']:.1f}% | {r['obs_read_commands']} | "
                f"{r['elapsed_s']:.0f} |"
            )

    reference = summary.get("__reference__")
    arms = [a for a in summary if not a.startswith("__") and a != reference]
    deltas = summary.get("__deltas__", {})
    if reference and arms:
        lines += ["", f"## 分组汇总（均值，参考臂 = {reference}）", ""]
        lines.append("| 指标 | " + reference + " | " + " | ".join(arms) + " |")
        lines.append("|---" * (1 + 1 + len(arms)) + "|")

        def row(label: str, key: str, fmt: str, delta_key: str | None = None, suffix: str = "") -> str:
            ref_row = summary[reference]
            cells = [fmt.format(ref_row[key])]
            for arm in arms:
                d = deltas.get(arm, {}).get(delta_key) if delta_key else None
                cells.append(fmt.format(summary[arm][key]) if d is None
                             else f"{fmt.format(summary[arm][key])} ({d:+}{suffix})")
            return f"| {label} | " + " | ".join(cells) + " |"

        lines.append(f"| 样本数 n | {summary[reference]['n']} | "
                    + " | ".join(str(summary[a]['n']) for a in arms) + " |")
        lines.append(row("任务成功率", "success_rate", "{:.1%}", "success_rate_pp", "pp"))
        lines.append(row("平均模型调用数", "mean_agent_calls", "{:.2f}"))
        lines.append(row("平均可见观测字符", "mean_visible_payload_chars", "{:.0f}"))
        lines.append(row("平均原始字符", "mean_raw_chars", "{:.0f}"))
        lines.append(row("平均重复命令率", "mean_repeat_rate_pct", "{:.1f}%", "repeat_rate_diff_pp", "pp"))
        lines.append(row("平均回读次数", "mean_obs_read_commands", "{:.2f}"))
        lines.append(row("平均耗时(s)", "mean_elapsed_s", "{:.0f}"))
        lines.append(f"| 触发截断/落盘总次数 | {summary[reference]['total_truncated_messages']} | "
                     + " | ".join(
                         # offload 臂的截断发生在环境层，truncated_messages 与 offload_count
                         # 记录的是同一批事件，只取 offload_count 避免重复计数
                         str(summary[a]['total_offload'] or summary[a]['total_truncated_messages'])
                         for a in arms) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=ARTIFACTS / "v13_strict")
    parser.add_argument("--reference", default="baseline",
                        help="对比基准臂（如 budget@2k）；不存在时退化为字典序第一个臂")
    args = parser.parse_args()

    runs = load_runs(args.run_root)
    if not runs:
        print(f"未找到任何 summary.json：{args.run_root}")
        return 1
    summary = summarize(runs, args.reference)
    markdown = render_markdown(runs, summary)

    out_md = args.run_root / "summary_report.md"
    out_md.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n已写入：{out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


