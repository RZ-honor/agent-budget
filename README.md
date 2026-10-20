# agent-budget · Agent 上下文预算治理与成本评测

> 在开源轻量内核 **mini-SWE-Agent** 上做**单点增量**：历史级上下文卸载 + 运行时收敛护栏 + 失败熔断。
> 用真实 SWE-bench 任务的**同任务配对实验**证明：模型输入成本显著下降，而解题成功率不退化。
>
> 项目自包含：第三方依赖随项目存放，**不需要 pip 安装、不需要联网**即可运行。

## 核心结果

| 指标 | 结果 |
|---|---|
| 模型输入成本（4 组同任务配对，中位） | **−41%** |
| 最高单组降幅 | 411 万 → 227 万字符 |
| 解题成功率 | 与未改造版本持平 |
| 严格协议复跑（隔离 test_patch、双门限判定） | **8/8 成功** |
| 对内核主循环的侵入 | 无，只覆写既有查询钩子 |

汇总快照由 `analysis/` 下的脚本生成到 `reports/`，逐次运行明细在 `artifacts/`。

## 问题与方案

长程任务里 Agent 的输入成本随步数线性增长：每一轮都把完整历史重新发给模型。提示词层面的约束解决不了这件事，只能在运行时动手。

三个机制各自解决一类问题，且都做成**单点接入**，不改主循环：

| 机制 | 位置 | 做法 |
|---|---|---|
| 历史级卸载 | `agents/offload.py` | 累计历史超预算时，把较早的工具观测落盘并回填文件指针，最近若干条保持完整，模型可按需回读 |
| 收敛护栏 | `guard.py` + `agents/guarded.py` | 同一调用签名直接拦截；不同参数但内容相同按观测哈希去重；达到阈值后移除工具描述，使模型只能输出文本 |
| 失败熔断 | `guard.py` | 按命令族统计连续失败，达到阈值进入冷却，半开恢复；参数校验前置到函数入口 |

## 目录结构

```
agent-budget/
├── src/agent_budget/
│   ├── guard.py              运行时护栏核心：签名拦截 / 观测去重 / 强制作答 / 按命令族熔断
│   ├── paths.py              落盘与依赖位置的唯一来源
│   ├── _bootstrap.py         把 third_party 挂到 sys.path（免安装）
│   ├── agents/
│   │   ├── offload.py        历史级卸载：超预算把早期观测换成文件指针
│   │   ├── guarded.py        收敛三层挂到 mini-swe-agent 默认循环（含 ForcedAnswer）
│   │   └── observation.py    观测渲染的分臂模板
│   ├── env/
│   │   ├── git_bash.py       Git-Bash 执行环境（超时 + 进程树强杀 + 卸载版）
│   │   └── task_setup.py     任务准备：本地化仓库副本，绕开沙箱内 git clone 失败
│   ├── run/
│   │   ├── arm.py            单臂运行器：建任务 → 起 Agent → 落盘 summary
│   │   └── matrix.py         交错矩阵编排：A/B 交替执行，消除顺序混杂
│   └── analysis/
│       ├── analyze.py        单任务三层断言与失败归因
│       ├── summarize.py      矩阵汇总，输出 A/B 对照表
│       ├── audit_runs.py     两种成功口径的判定（严格 / 标准语义）
│       ├── audit_guard.py    护栏触发数据汇总
│       └── p2p_precheck.py   干净克隆上测「环境自身的失败面」
├── third_party/              第三方依赖（源码形式，仅本地保留，不入库）
│   ├── pylibs/               litellm / pydantic / openai / jinja2 / yaml / httpx …
│   ├── mini-swe-agent/       开源轻量内核源码
│   └── agent-harness/        agentops 包（任务准备与 SWE-bench 实例解析）
├── scripts/
│   ├── run.ps1               运行入口：设好 PYTHONPATH 再转发给 Python
│   └── run_tests_manual.py   无 pytest 时直接跑不依赖 pytest 的用例
├── tests/                    16 条单元与口径测试
├── reports/                  汇总快照
├── logs/                     各轮矩阵运行日志
├── artifacts/                实验产物（未入库）
│   ├── v10/ v11_9359/ v12_guard/ v13_strict/   结论证据
│   ├── mimo/ mimo_v2/                          真实模型探测
│   ├── history/                                口径收敛期的中间轮
│   └── deprecated/                             作废跑（泄漏 / 无效 / 参数扫描）
└── legacy/pre_reorg/         早期版本脚本，仅本地对照，不入库
```

## 运行

依赖只靠 `PYTHONPATH` 生效，用包装脚本即可：

```powershell
cd agent-budget

# 汇总
.\scripts\run.ps1 -m agent_budget.analysis.summarize --run-root artifacts/v13_strict

# 口径审计
.\scripts\run.ps1 -m agent_budget.analysis.audit_runs
.\scripts\run.ps1 -m agent_budget.analysis.audit_guard

# 单任务归因
.\scripts\run.ps1 -m agent_budget.analysis.analyze --run-root artifacts/v13_strict

# 跑一次 agent
.\scripts\run.ps1 -m agent_budget.run.arm --arm baseline --task pylint-8281 --steps 2 \
    --run-root artifacts/smoke/r1

# 交错矩阵
.\scripts\run.ps1 -m agent_budget.run.matrix --tasks pylint-8281,pytest-9359 \
    --arms baseline,guard --reps 2

# 测试
.\scripts\run.ps1 -m pytest -q
```

也可以在代码里 `import agent_budget._bootstrap` 达到同样效果。

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 解释器 | **Python 3.10** | `third_party/pylibs` 内的二进制扩展均为 cp310，换版本会加载失败 |
| 安装 | 不需要 | 依赖以源码/包目录形式随项目存放 |
| 网络 | 运行不需要 | 仅在缺依赖时才需要补 |
| 模型 | OpenAI 兼容接口 | 通过 `--model` / 环境变量指定，密钥由 `--api-key-env` 指向的环境变量提供 |
| 任务数据 | SWE-bench 实例工作区 | 默认 `MSB_WORK_ROOT`，可覆盖 |

## 路径约定

所有位置集中在 `src/agent_budget/paths.py`，均可用环境变量覆盖：

| 常量 | 默认 | 覆盖变量 |
|---|---|---|
| `ARTIFACTS` | `<project>/artifacts` | `MSB_ARTIFACTS` |
| `REPORTS` | `<project>/reports` | `MSB_REPORTS` |
| `PYLIBS` | `<project>/third_party/pylibs` | `MSB_PYLIBS` |
| `VENDOR_MINI` | `<project>/third_party/mini-swe-agent` | `MSB_MINI_SWE` |
| `AGENT_HARNESS` | `<project>/third_party/agent-harness` | `MSB_AGENT_HARNESS` |
| 任务工作区 | `D:\PROJECT\github\_session_eval_repos\swebench_work` | `MSB_WORK_ROOT` |

## 评测协议

- **配对实验**：同一任务、同一随机种子下交替跑 baseline 与改造臂，消除「基线全先跑」的执行顺序混杂
- **双门限判定**：F2P（修复用例）全绿 **且** P2P（回归用例）不新增失败，才算成功
- **环境基线先行**：`p2p_precheck.py` 在未打补丁的干净克隆上先跑一遍，测出环境自身就会红的节点，判定时剔除
- **答案隔离**：`test_patch` 只用于冒烟，用完撤销并删除，避免题解泄漏给 Agent；执行层限制网络出口
