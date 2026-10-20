"""历史级卸载：上下文累计超过预算时，把较早的工具观测替换为文件指针。

对应主流内核已有的"上下文编辑/压缩"做法（上下文编辑、凝聚器这类机制）：
不改主循环，只在每次查询模型前重写一次历史，把"每步全量推送"变成"需要时按路径拉取"。

与单条观测截断的区别：
- 单条截断只在单条输出超长时触发，中小型任务几乎不触发；
- 历史级卸载由累计上下文驱动，只要对话变长就会触发，因此收益可稳定观测。
"""
from __future__ import annotations

from pathlib import Path

from minisweagent.agents.default import DefaultAgent


class HistoryOffloadAgent(DefaultAgent):
    """在默认循环上只加一个钩子：查询前按预算卸载较早观测。"""

    def __init__(self, *args, obs_dir: Path, budget_chars: int, keep_recent: int = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self.obs_dir = Path(obs_dir)
        # 预算按字符计，30k 字符约 7.5k 词元，与主流内核单条消息上限同量级
        self.budget_chars = budget_chars
        # 最近若干条观测保持完整，避免模型刚拿到的结果立刻被搬走
        self.keep_recent = keep_recent
        self.offloaded_messages = 0
        self._seq = 0

    def query(self):
        self._offload_history()
        return super().query()

    @staticmethod
    def _text(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return ""

    def _offload_history(self) -> None:
        """把最早的工具观测搬进文件，直到累计字符回落到预算内。"""
        tool_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        protected = set(tool_indices[-self.keep_recent:]) if self.keep_recent else set()
        total = sum(len(self._text(m)) for m in self.messages)
        if total <= self.budget_chars:
            return
        self.obs_dir.mkdir(parents=True, exist_ok=True)
        for index in tool_indices:
            if total <= self.budget_chars:
                break
            if index in protected:
                continue
            message = self.messages[index]
            content = self._text(message)
            # 已经卸载过或内容为空则跳过，避免重复写文件
            if not content.strip() or content.startswith("[history offload]"):
                continue
            self._seq += 1
            path = self.obs_dir / f"obs_hist_{self._seq:03d}.log"
            path.write_text(content, encoding="utf-8")
            pointer = (
                f"[history offload] 该条观测已写入 {path.as_posix()}（原始 {len(content)} 字符）\n"
                f"需要原文时按行检索：grep -n \"关键字\" {path.as_posix()} 再用 sed -n \"起,止p\" 取段\n"
                "以下为开头 200 字符：\n"
                + content[:200]
            )
            message["content"] = pointer
            total += len(pointer) - len(content)
            self.offloaded_messages += 1
