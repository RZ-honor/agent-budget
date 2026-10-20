from jinja2 import StrictUndefined, Template


BASELINE_OBSERVATION_TEMPLATE = """\
{% if output.exception_info %}<exception>{{ output.exception_info }}</exception>{% endif %}
<returncode>{{ output.returncode }}</returncode>
<output>
{{ output.output -}}
</output>
"""


def build_budget_template(threshold: int, head_ratio: float = 0.5) -> str:
    """生成截断观测模板：阈值与头尾比例可配，默认 10k/5:5 与上游一致。

    与落盘臂对比时传入相同阈值和 6:4 头尾比例，保证两臂唯一的差异
    是“中间部分被丢弃”还是“写入本地文件并给出检索入口”。
    """
    head = int(threshold * head_ratio)
    tail = threshold - head
    return f"""\
{{% if output.exception_info %}}<exception>{{{{ output.exception_info }}}}</exception>{{% endif %}}
<returncode>{{{{ output.returncode }}}}</returncode>
{{% if output.output | length < {threshold} -%}}
<output>
{{{{ output.output -}}}}
</output>
{{%- else -%}}
<warning>
The output of your last command was too long.
Please use a narrower command or redirect the full output to a file.
</warning>
{{%- set elided_chars = output.output | length - {threshold} -%}}
<output_head>
{{{{ output.output[:{head}] }}}}
</output_head>
<elided_chars>
{{{{ elided_chars }}}} characters elided
</elided_chars>
<output_tail>
{{{{ output.output[-{tail}:] }}}}
</output_tail>
{{%- endif -%}}
"""


# 上游 mini.yaml 默认行为：10k 阈值、头尾各 5k
BUDGET_OBSERVATION_TEMPLATE = build_budget_template(10000, 0.5)


def render_observation(arm: str, output_text: str) -> str:
    if arm not in {"baseline", "budget"}:
        raise ValueError(f"unsupported observation arm: {arm}")
    template = BASELINE_OBSERVATION_TEMPLATE if arm == "baseline" else BUDGET_OBSERVATION_TEMPLATE
    return Template(template, undefined=StrictUndefined).render(
        output={"output": output_text, "returncode": 0, "exception_info": ""}
    )
