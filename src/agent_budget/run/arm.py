from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

# 第三方依赖以源码形式放在 third_party/，统一由 _bootstrap 挂到 sys.path
import agent_budget._bootstrap  # noqa: F401,E402  (必须在 minisweagent 之前)

import yaml

from agent_budget.env.git_bash import GitBashEnvironment, OffloadGitBashEnvironment
from agent_budget.agents.guarded import GuardedAgent, SwitchableLitellmModel
from agent_budget.agents.offload import HistoryOffloadAgent
from agent_budget.env.task_setup import setup as setup_task
from agent_budget.guard import RuntimeGuard
from minisweagent.agents.default import DefaultAgent
from minisweagent.models.litellm_model import LitellmModel
from agent_budget.agents.observation import BASELINE_OBSERVATION_TEMPLATE, build_budget_template


from agent_budget.paths import AGENT_HARNESS, REPO as ROOT, VENDOR_MINI as MINI_REPO  # noqa: E402
ENV_FILE = AGENT_HARNESS / ".env"
MINI_CONFIG = MINI_REPO / "src" / "minisweagent" / "config" / "mini.yaml"


from agentops.session_eval import stage1_task_swe as dsh_runner  # noqa: E402
from agentops.session_eval import swe_task_setup as prep  # noqa: E402


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def observation_metrics(messages: list[dict]) -> dict[str, int]:
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    raw_chars = 0
    visible_chars = 0
    visible_payload_chars = 0
    truncated_messages = 0
    for message in tool_messages:
        extra = message.get("extra") or {}
        visible = message_text(message)
        # 两种截断标记：budget 臂由模板写入 "characters elided"，offload 臂由环境层写入 offload 提示
        truncated = (
            "characters elided" in visible
            or "[observation offload]" in visible
            or "[history offload]" in visible
        )
        # offload 臂的 raw_output 已是截断后文本，原始长度单独记录在 original_chars；
        # budget/baseline 臂的 raw_output 即完整输出，直接取其长度。
        original = extra.get("original_chars")
        msg_raw_chars = int(original) if isinstance(original, int) else len(str(extra.get("raw_output") or ""))
        raw_chars += msg_raw_chars
        visible_chars += len(visible)
        # 未截断时模型看到的就是原始输出；截断时以实际可见字符计
        visible_payload_chars += len(visible) if truncated else msg_raw_chars
        if truncated:
            truncated_messages += 1
    return {
        "observation_messages": len(tool_messages),
        "raw_chars": raw_chars,
        "visible_chars": visible_chars,
        "visible_payload_chars": visible_payload_chars,
        "elided_chars": max(raw_chars - visible_payload_chars, 0),
        "truncated_messages": truncated_messages,
    }


def command_metrics(messages: list[dict]) -> dict[str, int | float]:
    """统计命令总数、去重数与重复率。

    重复命令是“上一条观测被截断、模型拿不到所需信息”的直接信号，
    所以它比字符数更接近机制的实际效果。
    """
    commands: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            command = str(arguments.get("command") or "").strip()
            if command:
                commands.append(command)
    normalized = [re.sub(r"\s+", " ", command) for command in commands]
    unique = len(set(normalized))
    repeated = len(normalized) - unique
    return {
        "commands": len(commands),
        "unique_commands": unique,
        "repeated_commands": repeated,
        "repeat_rate_pct": round(repeated / len(commands) * 100, 2) if commands else 0.0,
        # 落盘机制被真正使用的次数：命令中出现 _obs/ 才算回读
        "obs_read_commands": sum(1 for command in commands if "_obs/" in command),
    }


def load_agent_templates() -> tuple[str, str]:
    config = yaml.safe_load(MINI_CONFIG.read_text(encoding="utf-8"))
    agent = config["agent"]
    return agent["system_template"], agent["instance_template"]


def build_task(task: str, meta: dict, env: dict, workdir: Path) -> str:
    return f"""\
{meta['problem_statement']}

Environment:
- The current working directory is already this repository: {workdir.as_posix()}
- Do NOT cd to the repository path again. Start with `pwd` and `ls`.
- The bash tool runs through Git Bash on Windows. Use POSIX commands such as ls, grep, sed, git, and python.
- Test interpreter: {env['python']}
- Package path: {env['pythonpath']}
- Run focused tests with: PYTHONPATH={env['pythonpath']} "{env['python']}" -m pytest -q <test-file-or-node>
- Do not modify test files. Only change source files needed to fix the issue.
- The project's own test suite is present, but the failing check for this issue is not: write your own small reproduction if you need to verify the behaviour.
- Finish by running exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def restore_test_patch(workdir: Path, meta: dict) -> None:
    for rel in dsh_runner.test_file_paths(meta["test_patch"]):
        subprocess.run(["git", "-C", str(workdir), "checkout", "--", rel], check=False)
    (workdir / "test_patch.diff").write_text(meta["test_patch"], encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workdir), "apply", "test_patch.diff"],
        check=False,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("baseline", "budget", "offload", "hist", "guard"), required=True)
    parser.add_argument("--task", default="pytest-11148")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8899/v1")
    parser.add_argument("--model", default="deepseek-v4.1-flash-1017")
    parser.add_argument("--api-key-env", default="QWEN_API_KEY")
    parser.add_argument("--api-base-env", default="QWEN_BASE_URL")
    parser.add_argument("--model-env", default="QWEN_MODEL")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--cmd-timeout", type=int, default=600,
                        help="单条命令超时（秒），与运行墙钟解耦；超时命令整树终止并以观测形式返回")
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--obs-threshold", type=int, default=10000,
                        help="offload 臂的超长输出阈值（字符）")
    parser.add_argument("--budget-threshold", type=int, default=10000,
                        help="budget 臂截断阈值（字符）；与 offload 同阈值时只差丢弃还是落盘")
    parser.add_argument("--budget-head-ratio", type=float, default=0.5,
                        help="budget 臂头段占比，默认 0.5 与上游 mini.yaml 一致")
    parser.add_argument("--history-budget", type=int, default=30000,
                        help="hist 臂的上下文累计预算（字符），30k 约 7.5k 词元，与主流单条消息上限同量级")
    parser.add_argument("--force-answer-after", type=int, default=50,
                        help="guard 臂的强制作答阈值（已执行的工具调用次数）；0 表示不强制")
    parser.add_argument("--breaker-threshold", type=int, default=3,
                        help="guard 臂的命令族连续工具层失败熔断阈值")
    parser.add_argument("--breaker-cooldown", type=int, default=300,
                        help="guard 臂熔断冷却秒数")
    args = parser.parse_args()

    env_values = load_env()
    api_key = env_values.get(args.api_key_env) or os.environ.get(args.api_key_env, "")
    api_base = env_values.get(args.api_base_env) or os.environ.get(args.api_base_env, args.proxy_url)
    model_name = env_values.get(args.model_env) or os.environ.get(args.model_env, args.model)
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is missing")

    # 必须解析为绝对路径：PYTHONPATH 会传入 Git Bash 子进程，
    # 相对路径在 bash 的工作目录（work/）下会解析失败，
    # 导致测试加载 site-packages 的旧代码而非本次改动的源码。
    run_dir = (args.run_root / args.arm / args.task).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = run_dir / "work"
    task_env = setup_task(args.task, workdir)
    meta = json.loads((prep.WORK / "task_meta.json").read_text(encoding="utf-8"))[args.task]

    # 隔离元数据：_env.json 里记录着评测目录路径，agent 可顺着它找到 gold 补丁并直接照抄，
    # 因此进入模型循环前必须删除，避免通过率失真。
    (workdir / "_env.json").unlink(missing_ok=True)
    # 数据完整性前置检查：评测目录若残留标准答案，直接中止，避免产出不可信数据。
    leaked = [str(p) for p in prep.WORK.glob("**/gold*.diff")]
    if leaked:
        raise SystemExit("检测到标准答案残留，请先移出评测目录：" + ", ".join(leaked))
    # task_meta.json 的 patch 字段同样是标准答案，运行时若仍存在则直接中止。
    if "patch" in meta:
        raise SystemExit("task_meta.json 仍包含 patch 字段，请先做脱敏处理")

    system_template, instance_template = load_agent_templates()
    task_text = build_task(args.task, meta, task_env, workdir)
    # budget 臂阈值可配：与 offload 臂同阈值时，两臂唯一差异是中间部分是丢弃还是落盘
    if args.arm == "budget":
        observation_template = build_budget_template(args.budget_threshold, args.budget_head_ratio)
    else:
        observation_template = BASELINE_OBSERVATION_TEMPLATE

    model_class = SwitchableLitellmModel if args.arm == "guard" else LitellmModel
    model = model_class(
        model_name=f"openai/{model_name}",
        model_kwargs={
            "base_url": api_base,
            "api_key": api_key,
            "temperature": 0,
            "drop_params": True,
            "max_tokens": 16384,
            "timeout": 180,
        },
        cost_tracking="ignore_errors",
        observation_template=observation_template,
    )
    # 成本打点：包一层模型查询，只记录每次实际发送的历史字符数，不改变调用行为
    prompt_chars: list[int] = []
    _raw_query = model.query

    def _counting_query(messages, *q_args, **q_kwargs):
        prompt_chars.append(sum(len(message_text(m)) for m in messages))
        return _raw_query(messages, *q_args, **q_kwargs)

    model.query = _counting_query
    mini_config = yaml.safe_load(MINI_CONFIG.read_text(encoding="utf-8"))
    env_kwargs = dict(
        cwd=str(workdir),
        timeout=args.cmd_timeout,
        env={
            **mini_config.get("environment", {}).get("env", {}),
            "PYTHONPATH": task_env["pythonpath"],
            "PYTHONIOENCODING": "utf-8",
            "PATH": r"C:\Program Files\Git\bin;" + os.environ.get("PATH", ""),
        },
    )
    # offload 臂截断发生在环境层（落盘 + 给出可检索路径），观测模板保持完整透传；
    # budget 臂截断发生在模板层（等价于上游 mini.yaml 默认行为），作为机制对照。
    if args.arm in ("offload", "guard"):
        # 头尾长度必须小于阈值，否则截断后头尾会重叠、可见字符反而变多；
        # 按 6:4 比例分配，保证任意阈值下都自洽（10k 阈值即头 6000 + 尾 4000）。
        head_chars = int(args.obs_threshold * 0.6)
        tail_chars = args.obs_threshold - head_chars
        environment = OffloadGitBashEnvironment(
            obs_dir=workdir / "_obs",
            threshold=args.obs_threshold,
            head_chars=head_chars,
            tail_chars=tail_chars,
            **env_kwargs,
        )
    else:
        environment = GitBashEnvironment(**env_kwargs)
    output_path = run_dir / "trajectory.json"
    agent_kwargs = dict(
        model=model,
        env=environment,
        system_template=system_template,
        instance_template=instance_template,
        step_limit=args.steps,
        cost_limit=0,
        output_path=output_path,
    )
    # hist 臂：上下文累计超预算时把较早观测搬进文件，模型可按路径回读。
    # 这一层不动主循环，只复用默认智能体的查询钩子。
    if args.arm == "hist":
        agent = HistoryOffloadAgent(
            obs_dir=workdir / "_obs_history",
            budget_chars=args.history_budget,
            keep_recent=3,
            **agent_kwargs,
        )
    elif args.arm == "guard":
        # guard 臂 = 预算 + 收敛三层 + 熔断，即运行时完整形态
        guard = RuntimeGuard(
            force_answer_after=args.force_answer_after,
            breaker_threshold=args.breaker_threshold,
            breaker_cooldown_s=float(args.breaker_cooldown),
        )
        agent = GuardedAgent(
            guard=guard,
            obs_dir=workdir / "_obs_history",
            budget_chars=args.history_budget,
            keep_recent=3,
            **agent_kwargs,
        )
    else:
        agent = DefaultAgent(**agent_kwargs)

    started = time.time()
    error = ""
    info: dict = {}
    try:
        info = agent.run(task_text)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.time() - started, 2)

    diff = subprocess.run(
        ["git", "-C", str(workdir), "diff", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    (run_dir / "agent.diff").write_text(diff, encoding="utf-8")
    restore_test_patch(workdir, meta)

    f2p = dsh_runner.read_nodes(Path(task_env["f2p"]))
    p2p = dsh_runner.read_nodes(Path(task_env["p2p_gate"]))
    post_f2p = dsh_runner.run_nodes(task_env, workdir, f2p)
    post_p2p = dsh_runner.run_nodes(task_env, workdir, p2p)
    f2p_ok = post_f2p["failed"] == 0 and post_f2p["passed"] == len(f2p)
    p2p_ok = post_p2p["failed"] == 0

    metrics = observation_metrics(agent.messages)
    commands = command_metrics(agent.messages)
    metrics["payload_reduction_pct"] = (
        round((1 - metrics["visible_payload_chars"] / metrics["raw_chars"]) * 100, 2)
        if metrics["raw_chars"]
        else 0.0
    )
    result = {
        "arm": args.arm,
        "task": args.task,
        # 记录本次阈值，便于汇总时区分不同预算档位的对照
        "obs_threshold": args.obs_threshold,
        "budget_threshold": args.budget_threshold,
        "budget_head_ratio": args.budget_head_ratio,
        "model": model_name,
        "workdir": str(workdir),
        "status": "error" if error else "ok",
        "error": error,
        "agent_info": info,
        "agent_calls": agent.n_calls,
        "agent_cost": agent.cost,
        "elapsed_s": elapsed,
        "messages": len(agent.messages),
        "observation": metrics,
        "commands": commands,
        "f2p": post_f2p,
        "p2p": post_p2p,
        "f2p_pass": f2p_ok,
        "p2p_pass": p2p_ok,
        "success": bool(f2p_ok and p2p_ok),
        "diff_chars": len(diff),
        # offload 臂的实际落盘次数，用于判断该机制本轮是否被触发
        "offload_count": getattr(environment, "offload_count", 0),
        # hist 臂的历史卸载条数；其它臂为 0
        "history_budget": args.history_budget,
        "history_offloaded": getattr(agent, "offloaded_messages", 0),
        # 真实成本口径：每次调用模型时整段历史的字符数，跨步累加
        "prompt_chars_total": sum(prompt_chars),
        "prompt_chars_per_call": round(sum(prompt_chars) / len(prompt_chars), 1) if prompt_chars else 0,
        # guard 臂：收敛三层与熔断在真实任务上的触发记录；其它臂为 None
        "guard": agent.guard.summary() if args.arm == "guard" else None,
    }
    (run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("arm", "task", "status", "success", "f2p_pass", "p2p_pass", "agent_calls", "elapsed_s")} | {"observation": metrics}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
