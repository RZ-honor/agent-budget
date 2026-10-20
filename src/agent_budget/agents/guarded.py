# -*- coding: utf-8 -*-
"""把收敛三层与失败熔断挂到 mini-swe-agent 的默认循环上。

接入点只有两个，主循环与工具链不改：
  · query()           —— 工具调用到阈值后停用工具描述，模型只能出文本
  · execute_actions() —— 执行前判签名/熔断，执行后折叠重复观测

模型侧只多一个开关：关掉 tools 参数即可让模型无法再发起工具调用，
这是"移出工具描述"的直接实现，比在提示词里禁止调用可靠。
"""
from __future__ import annotations

import litellm

from agent_budget.agents.offload import HistoryOffloadAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow
from minisweagent.models.litellm_model import LitellmModel
from agent_budget.guard import RuntimeGuard

FORCE_ANSWER_NOTE = (
    "工具调用预算已用尽，工具已停用。请直接用一两句话给出：根因是什么、改了哪些文件，"
    "然后结束本次任务。不要再请求执行命令。"
)


class ForcedAnswer(InterruptAgentFlow):
    """工具停用后的收尾。

    不能靠 FormatError 自然退出：工具一停用，模型只能出文本，而解析层把「没有工具调用」
    当成格式错误，连错 3 次后以 RepeatedFormatError 收场——那是错误退出，不是答题。
    实测 12-09 那次 pylint 就栽在这里：强制作答触发后本该收尾，却报 RepeatedFormatError，
    补丁只写了一半。因此这里显式把纯文本当作终稿。"""


class SwitchableLitellmModel(LitellmModel):
    """tools_enabled=False 时不再下发工具描述，模型只能输出文本。"""

    tools_enabled: bool = True

    def _query(self, messages, **kwargs):
        if self.tools_enabled:
            return super()._query(messages, **kwargs)
        return litellm.completion(
            model=self.config.model_name,
            messages=messages,
            **(self.config.model_kwargs | kwargs),
        )


class GuardedAgent(HistoryOffloadAgent):
    def __init__(self, *args, guard: RuntimeGuard, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard
        self.executed_actions = 0
        self.tools_disabled = False

    def query(self) -> dict:
        if not self.tools_disabled and self.guard.should_force_answer(self.executed_actions):
            self.tools_disabled = True
            self.model.tools_enabled = False
            self.add_messages(self.model.format_message(role="user", content=FORCE_ANSWER_NOTE))
        try:
            return super().query()
        except FormatError as e:
            if not self.tools_disabled:
                raise
            # 工具已停用，模型只能输出文本，解析层必然拿不到工具调用。
            # 这时把这段文本当作提交，而不是让它累计成 RepeatedFormatError。
            text = next(
                (m.get("content") for m in getattr(e, "messages", ())
                 if isinstance(m.get("content"), str) and m.get("content").strip()),
                "",
            )
            raise ForcedAnswer(
                {
                    "role": "exit",
                    "content": "ForcedAnswer",
                    "extra": {"exit_status": "ForcedAnswer", "submission": text},
                }
            ) from None

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", []) or []
        # 工具已停用且模型不再发起调用 → 这一轮文本就是终稿，直接收尾
        if self.tools_disabled and not actions:
            return self.add_messages(
                {
                    "role": "exit",
                    "content": "ForcedAnswer",
                    "extra": {"exit_status": "ForcedAnswer", "submission": message.get("content") or ""},
                }
            )
        outputs = []
        for action in actions:
            command = action.get("command", "")
            decision = self.guard.pre_action(command)
            if decision.kind != "execute":
                outputs.append(
                    {
                        "output": decision.reason,
                        "returncode": 0,
                        "exception_info": "",
                        "extra": {"guard": decision.kind},
                    }
                )
                continue
            self.executed_actions += 1
            output = self.env.execute(action)
            text, folded = self.guard.post_action(
                command, output.get("output") or "", int(output.get("returncode", 0))
            )
            if folded:
                output["output"] = text
                output.setdefault("extra", {})["folded"] = True
            outputs.append(output)
        return self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )