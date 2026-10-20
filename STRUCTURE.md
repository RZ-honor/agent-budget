# agent-budget · 架构与目录结构

> 项目根目录：仓库根（本文件与 `src/` 同级）
> 代码规模：13 个模块 + 16 条单元与口径测试

---

## 1. 分层

| 层 | 模块 | 职责 |
|---|---|---|
| ① 任务与环境 | `env/task_setup.py`、`env/git_bash.py` | 准备干净的仓库副本、执行命令、超时与进程树强杀 |
| ② Agent 适配 | `agents/offload.py`、`agents/guarded.py`、`agents/observation.py` | 只覆写查询钩子与工具描述，不改主循环 |
| ③ 运行时护栏 | `guard.py` | 三层收敛 + 失败熔断的决策核心，无 IO、纯函数为主，便于单测 |
| ④ 实验编排 | `run/arm.py`、`run/matrix.py` | 任务构建、模型调用、指标采集、A/B 交错执行 |
| ⑤ 归因与审计 | `analysis/*.py` | 不重跑模型，只对已落盘轨迹做判定、配对与显著性 |

数据流：

```
matrix 排列组合
   → arm 建任务、装配 Agent（offload / guarded 挂载）
   → 外部脚本判定 F2P / P2P
   → artifacts/<版本>/<rep>/<臂>/<任务>/{summary.json, trajectory.json, agent.diff, work/}
   → analyze（单任务归因）
   → summarize（A/B 对照表）
   → audit_runs / audit_guard（口径判定与护栏统计）
```

---

## 2. 目录

```
.
├── README.md
├── STRUCTURE.md                     本文件
├── pyproject.toml                   src 布局 + pytest 配置
├── requirements.txt
├── src/agent_budget/
│   ├── guard.py                     护栏核心
│   ├── paths.py                     落盘与依赖位置的唯一来源
│   ├── _bootstrap.py                把 third_party 挂到 sys.path
│   ├── agents/{offload,guarded,observation}.py
│   ├── env/{task_setup,git_bash}.py
│   ├── run/{arm,matrix}.py
│   └── analysis/{analyze,summarize,audit_runs,audit_guard,p2p_precheck}.py
├── tests/                           test_guard / test_observation / test_metrics / test_git_bash_env
├── scripts/                         run.ps1（运行入口）、run_tests_manual.py
├── reports/                         汇总快照（summary / recheck / p2p 基线 / guard 汇总）
├── logs/                            各轮矩阵运行日志
├── third_party/                     第三方依赖（本地保留，不入库）
│   ├── pylibs/                      纯 Python 依赖包目录
│   ├── mini-swe-agent/              开源轻量内核源码
│   └── agent-harness/               agentops 包
├── artifacts/                       实验产物（本地保留，不入库）
│   ├── v10/ v11_9359/ v12_guard/ v13_strict/   结论证据
│   ├── mimo/ mimo_v2/                          真实模型探测
│   ├── history/                                口径收敛期的中间轮
│   └── deprecated/                             作废跑
└── legacy/pre_reorg/                早期版本脚本，仅本地对照
```

---

## 3. 依赖组织

依赖不安装，直接以源码/包目录形式放在 `third_party/` 下，运行期通过 `PYTHONPATH` 生效：

| 目录 | 内容 | 说明 |
|---|---|---|
| `third_party/pylibs` | litellm、openai、pydantic、jinja2、yaml、httpx、rich、tiktoken、aiohttp 等 | 含 cp310 二进制扩展，必须配 Python 3.10 |
| `third_party/mini-swe-agent` | 轻量内核源码（`src/minisweagent`） | 被覆写钩子的对象 |
| `third_party/agent-harness` | `agentops` 包 | 提供任务准备与 SWE-bench 实例解析，已剔除运行数据 |

`src/agent_budget/_bootstrap.py` 负责把上述路径插到 `sys.path`；`scripts/run.ps1` 是命令行层包装。

**为什么这么做**：二进制扩展与解释器版本绑定，做成可复制的目录比每次重建虚拟环境更可预测；也让项目在没有网络的机器上直接可运行。

---

## 4. 数据与产物

| 路径 | 内容 |
|---|---|
| `artifacts/<版本>/<rep>/<臂>/<任务>/summary.json` | 单任务判定结果与成本计量 |
| `.../trajectory.json` | 全轨迹（工具调用、观测长度、退出状态） |
| `.../agent.diff` | Agent 产出的补丁 |
| `.../work/` | 任务工作副本 |
| `reports/summary_*.json` | 汇总快照（配对降幅、成功率） |
| `reports/p2p_precheck.json` | 干净树上的失败节点基线 |
| `reports/guard_summary.json` | 护栏触发统计 |

产物与代码分离：改代码不需要动产物，换产物不需要改代码；`analysis/*` 全部只读产物。

---

## 5. 评测口径

- **严格口径**：F2P 全绿 且 P2P 门限全绿（需剔除干净树上就红的节点，见 `analysis/audit_runs.py` 的逐条说明）
- **标准语义口径**：F2P 全绿 且 post 失败的 P2P 节点 ⊆ 干净树上已失败的节点，即没有把原本能过的测试弄红
- **timeout / missing_summary** 单独列出，不混进分母
