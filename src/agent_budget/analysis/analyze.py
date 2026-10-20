# -*- coding: utf-8 -*-
"""对已归档的运行结果做三层断言与失败归因（纯后处理，不重跑模型）。

输入只读：summary.json（门限结果）、trajectory.json（命令与退出状态）、agent.diff（改动）。
因此可以在任意批次跑完之后重复执行，结论随产物走。

用法：
  python -m agent_budget.analysis.analyze --run-root artifacts/v13_strict
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SWE-bench 实例工作区（task_meta.json / f2p_resolved.txt 所在目录）。
# 默认取用户目录下的 swebench_work，可用 MSB_WORK_ROOT 覆盖。
DEFAULT_WORK_ROOT = Path(os.environ.get("MSB_WORK_ROOT") or (Path.home() / "swebench_work"))

# 测试文件：命中即视为改了测试，结果为不可信
TEST_FILE_PATTERN = re.compile(r"(^|/)(tests?)/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$")
# 越界命令：标准答案、评测元数据、外网抓取、仓库远端操作
FORBIDDEN_PATTERN = re.compile(
    r"gold|task_meta\.json|_env\.json|curl\s|wget\s|https?://|git\s+(fetch|pull|clone|remote)"
)
# 必须与 git_bash_environment.GUARD_MARK 保持一致：命中闸门的命令会带回这个标记
GUARD_MARK = "[实验装置拦截]"


def patch_paths(diff_text: str) -> list[str]:
    """从统一 diff 中提取被改动文件路径；/dev/null 代表新增或删除，跳过。"""
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            if match:
                paths.append(match.group(2))
        elif line.startswith("+++ b/"):
            paths.append(line[len("+++ b/"):].strip())
    # 去重同时保留顺序，避免同一文件被 diff --git 与 +++ 行重复计入
    return list(dict.fromkeys(p for p in paths if p and p != "/dev/null"))


def collect_command_observations(messages: list[dict]) -> list[tuple[str, str]]:
    """抽取（命令, 该命令的观测）配对。

    只统计命令本身无法区分"被闸门拦住"与"真的抓到了"，
    必须看紧随其后的观测里有没有闸门标记。
    """
    pairs: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            command = str(arguments.get("command") or "").strip()
            if not command:
                continue
            following = messages[index + 1] if index + 1 < len(messages) else {}
            observation = str(following.get("content") or "") if following.get("role") == "tool" else ""
            pairs.append((command, observation))
    return pairs


def test_patch_paths(work_root: Path, task: str) -> set[str]:
    """官方测试补丁涉及的文件，属于装置预置改动，不能算作模型改了测试。"""
    meta_file = work_root / "task_meta.json"
    if not meta_file.exists():
        return set()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    patch = (meta.get(task) or {}).get("test_patch") or ""
    return set(patch_paths(patch))


def f2p_test_files(task: str, work_root: Path) -> list[str]:
    """从失败用例节点清单反推测试文件路径，用于判断模型是否跑过相关测试。"""
    node_file = work_root / task / "f2p_resolved.txt"
    if not node_file.exists():
        return []
    files = []
    for line in node_file.read_text(encoding="utf-8").splitlines():
        node = line.strip()
        if node:
            files.append(node.split("::")[0])
    return files


def has_environment_error(messages: list[dict]) -> bool:
    """观测中若出现异常标签，说明有命令在环境层就崩了，而非逻辑判断错误。"""
    for message in messages:
        if message.get("role") == "tool" and "<exception>" in str(message.get("content") or ""):
            return True
    return False


def classify(row: dict) -> str:
    """五类规则化归因：越界优先作废，其次预算，成功不再归因。

    顺序敏感：越界与预算耗尽会掩盖真实的能力差异，必须先判。
    被闸门拦住的尝试不算作废（没有拿到信息），只有真正泄露才算。
    """
    if row["leaked_commands"] > 0 or row["changed_tests"]:
        return "越界作废"
    if row["step_limit_hit"]:
        return "预算耗尽"
    if row["success"]:
        return "通过"
    if not row["changed_files"]:
        return "定位失败"
    if not row["ran_f2p_test"]:
        return "验证误判"
    if row["environment_error"]:
        return "环境异常"
    return "修改错误"


def analyze_one(summary_path: Path, work_root: Path) -> dict | None:
    run_dir = summary_path.parent
    trajectory_path = run_dir / "trajectory.json"
    if not trajectory_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    messages = trajectory.get("messages") or []
    info = trajectory.get("info") or {}
    config = info.get("config") or {}
    step_limit = int(((config.get("agent") or {}).get("step_limit")) or 0)

    diff_path = run_dir / "agent.diff"
    # agent.diff 为空表示模型没有产出任何改动，是“定位失败”的强信号
    paths = patch_paths(diff_path.read_text(encoding="utf-8")) if diff_path.exists() else []
    pairs = collect_command_observations(messages)
    commands = [command for command, _ in pairs]
    test_files = f2p_test_files(summary_path.parent.name, work_root)
    preset_tests = test_patch_paths(work_root, summary_path.parent.name)
    # 官方测试补丁是装置预先贴上的，会出现在改动里；只有补丁之外的测试文件被改才算越界
    model_paths = [p for p in paths if p not in preset_tests]
    leaked = sum(1 for command, observation in pairs
                 if FORBIDDEN_PATTERN.search(command) and GUARD_MARK not in observation)

    row = {
        "arm": summary_path.parent.parent.name,
        "task": summary_path.parent.name,
        "rep": summary_path.parent.parent.parent.name,
        "success": bool(summary.get("success")),
        "f2p_pass": bool(summary.get("f2p_pass")),
        "p2p_pass": bool(summary.get("p2p_pass")),
        "agent_calls": summary.get("agent_calls") or 0,
        "exit_status": info.get("exit_status") or "",
        "step_limit": step_limit,
        "step_limit_hit": bool(step_limit and (summary.get("agent_calls") or 0) >= step_limit),
        "changed_files": len(model_paths),
        "changed_tests": any(TEST_FILE_PATTERN.search(p) for p in model_paths),
        "forbidden_commands": sum(1 for c in commands if FORBIDDEN_PATTERN.search(c)),
        "leaked_commands": leaked,
        "ran_f2p_test": any(f in c for c in commands for f in test_files),
        "environment_error": has_environment_error(messages),
    }
    row["verdict"] = classify(row)
    return row


def render(rows: list[dict]) -> str:
    lines = ["# 三层断言与失败归因", ""]
    lines += ["| 轮次 | 臂 | 任务 | 模型调用 | 改动文件 | 改了测试 | 受限命令 | 未拦截越界 | 跑到失败用例 | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["rep"], x["arm"], x["task"])):
        lines.append(
            f"| {r['rep']} | {r['arm']} | {r['task']} | {r['agent_calls']} | {r['changed_files']} | "
            f"{'是' if r['changed_tests'] else '否'} | {r['forbidden_commands']} | {r['leaked_commands']} | "
            f"{'是' if r['ran_f2p_test'] else '否'} | {r['verdict']} |"
        )

    lines += ["", "## 归因分布", "", "| 判定 | 次数 |", "|---|---|"]
    for verdict, count in Counter(r["verdict"] for r in rows).most_common():
        lines.append(f"| {verdict} | {count} |")

    lines += ["", "## 按臂汇总", "", "| 臂 | 样本 | 通过 | 平均调用 | 改动文件均值 | 越界 |", "|---|---|---|---|---|---|"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["arm"]].append(r)
    for arm, items in sorted(groups.items()):
        passed = sum(1 for r in items if r["success"])
        lines.append(
            f"| {arm} | {len(items)} | {passed} | "
            f"{statistics.fmean(r['agent_calls'] for r in items):.1f} | "
            f"{statistics.fmean(r['changed_files'] for r in items):.1f} | "
            f"{sum(r['forbidden_commands'] for r in items)} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    args = parser.parse_args()

    rows = [row for p in sorted(args.run_root.glob("r*/**/summary.json"))
            if (row := analyze_one(p, args.work_root))]
    if not rows:
        print(f"没有可分析的完成运行：{args.run_root}")
        return 1

    out_jsonl = args.run_root / "analysis.jsonl"
    out_md = args.run_root / "analysis_report.md"
    out_jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    markdown = render(rows)
    out_md.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n已写入：{out_jsonl}\n已写入：{out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
