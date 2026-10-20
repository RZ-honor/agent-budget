from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig


# 实验装置的安全闸：禁止联网取上游修复、禁止读取评测元数据。
# 只做事后扫描不够——一次成功的抓取就足以让该轮结果不再能归因于内核策略，
# 所以必须在执行层直接拒绝。两个实验臂共用同一闸门，条件保持对称。
NETWORK_GUARD = re.compile(
    r"\b(curl|wget|nc|ssh|scp|socat)\b"
    r"|git\s+(fetch|pull|clone|ls-remote|remote\s+add)"
    r"|pip\s+(install|download)|python\s*[-\w]*\s+-m\s+pip"
    r"|task_meta\.json|_eval_meta_archive|gold\.diff|gold_patch",
    re.IGNORECASE,
)
GUARD_MARK = "[实验装置拦截]"


class GitBashEnvironmentConfig(LocalEnvironmentConfig):
    bash_executable: str = os.environ.get("MSB_BASH") or r"C:\Program Files\Git\bin\bash.exe"


class GitBashEnvironment(LocalEnvironment):
    def __init__(self, *, config_class: type = GitBashEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        # 命中受限操作时直接返回拒绝观测，不进入子进程，保证两臂都无法联网或读元数据
        blocked = NETWORK_GUARD.search(command)
        if blocked:
            return {
                "output": (
                    f"{GUARD_MARK} 命令包含受限操作「{blocked.group(0)}」，已被拒绝执行。\n"
                    "本实验禁止联网与读取评测元数据，请只基于当前仓库内的文件定位缺陷。\n"
                ),
                "returncode": 1,
                "exception_info": "",
            }
        try:
            result = _run(
                command,
                cwd,
                os.environ | self.config.env,
                timeout or self.config.timeout,
                self.config.bash_executable,
            )
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as exc:
            raw_output = getattr(exc, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace")
                if isinstance(raw_output, bytes)
                else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        self._check_finished(output)
        return output


class OffloadGitBashEnvironment(GitBashEnvironment):
    """超长工具输出落盘 + 可检索回退。

    与上游 mini-swe-agent 默认策略的区别：上游在观测模板里直接丢弃超长内容
    （只保留头尾各 5000 字符），模型丢失信息后只能重跑命令，浪费步数。
    本实现把完整输出写入仓库内 _obs/ 目录，并在观测中给出相对路径，
    模型可用 grep/sed 定点检索，信息不丢失且可复现。
    """

    def __init__(
        self,
        *,
        obs_dir: Path,
        threshold: int = 10000,
        head_chars: int = 3000,
        tail_chars: int = 1000,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._obs_dir = Path(obs_dir)
        self._obs_dir.mkdir(parents=True, exist_ok=True)
        self._threshold = threshold
        self._head_chars = head_chars
        self._tail_chars = tail_chars
        self._seq = 0
        self.offload_count = 0      # 落盘次数，写入 summary 作为增量指标

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        output = super().execute(action, cwd, timeout=timeout)
        text = output.get("output") or ""
        if len(text) <= self._threshold:
            return output

        self._seq += 1
        name = f"obs_{self._seq:03d}.log"
        (self._obs_dir / name).write_text(text, encoding="utf-8")
        rel = f"_obs/{name}"
        # 截断前的原始长度必须单独留存：观测模板拿到的 output 已经是截断文本，
        # 若不记录，统计时会把"截断后长度"当作"原始长度"，导致削减比例失真。
        # extra 会由 format_toolcall_observation_messages 合并进消息的 extra 字段。
        output.setdefault("extra", {})["original_chars"] = len(text)
        output["output"] = (
            f"{text[: self._head_chars]}\n"
            f"[observation offload] 原始输出共 {len(text)} 字符，完整内容已写入 {rel}；"
            f"请用 `grep -n '<关键词>' {rel}` 或 `sed -n '起始行,结束行p' {rel}` 定点检索，不要重复执行原命令。\n"
            f"{text[-self._tail_chars:]}"
        )
        self.offload_count += 1
        return output


def _kill_tree(pid: int) -> None:
    """Windows 下按进程树强杀：bash 的 pytest 孙进程若存活会占住 stdout 管道。

    taskkill /T 递归终止整棵树，/F 强制；失败不抛异常，避免掩盖原超时错误。
    """
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _run(
    command: str,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    bash_executable: str,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [bash_executable, "-lc", command],
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        try:
            # 整棵进程树被终止后管道写端关闭，communicate 才能真正返回；
            # 之前只 kill bash 会被存活的 pytest 孙进程占住管道而永久阻塞。
            stdout, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout = ""
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)

