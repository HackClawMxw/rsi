# RSI Lab — skill 演进外环

> **当前状态**：提供离线复盘、circle/search 和原始 session 驱动的自主服务。自主模式调用 Codex 分析原始轨迹、提出假设、修改候选 skill、执行成对验证、独立审评，并将新 session 与失败反馈带入下一轮。框架行为测试与真实 Day0 改进效果分开验收；后者需要真实任务证据。

## 自主服务：从原始 Codex session 开始

```bash
python3 -m rsi_service serve --config examples/autonomous/config.json
python3 -m rsi_service status --config examples/autonomous/config.json
```

这个示例会实际调用本机已配置的 Codex 模型，运行一个有预算的本地 CPU 协议任务；它不是 NPU/Day0 验收，也不保证候选一定优于原版。需要可用的 Codex CLI 和模型连接。配置中的 `objective` 固定优化目标、指标和门禁，`autonomy.source` 指定原始 rollout 文件或准确的 session ID 与搜索目录。无需先手工写 `self-evolving.md`，也无需编写一个占位的外部 Day0 harness 才能调度模型任务。

```text
原始 session → 事件索引/原文 → 模型分析 → 改进假设 → 候选 skill
     ↑                                               ↓
     └── 新 session + 学习记录 ← 复盘/晋升或拒绝 ← 执行与审评
```

分析、计划、修改、执行、审评、复盘是分开的模型任务。控制器拥有预算与晋升规则；固定检查命令可为业务门禁增加机械验证。基线与候选从相同本地工作区快照开始，候选验证本身就是新一轮执行，晋升后不会为了“部署”再重复执行一次。每轮保留 `learning.json`，记录假设、比较结果、反例和下一方向。

使用方式与边界见 [自主服务说明](docs/AUTONOMOUS.md)，模块划分与状态契约见 [架构设计](docs/ARCHITECTURE.md)。旧 circle/search 模式保持兼容。

本仓是 AscendBot 系列 skill 的**演进基础设施**：聚合 run 证据、维护演进账本、裁决演进候选、验证 skill 变更。它**不装进目标仓、不是任何 skill 的运行时依赖**——lab 不在，skill 照常干活，只是没人记账。

## 三方分工

```
AscendBot（skill 仓）          目标仓（vllm-ascend 等）        本仓（lab）
──────────────                ──────────────────            ──────────
方法论本体                      run 真正发生的地方              聚合 · 裁决 · 验证
被优化的对象                    tracker / closeout / .day0/    runs/ 快照 + ledger/ 账本
只实现两个契约（CONTRACT.md）    探针在此采集                    candidates/ 队列 + canaries/
```

数据流向：目标仓 closeout 产出 `run_metrics.json` → `scripts/import_run.py` 导入 `runs/` → `ledger/` 聚合成成本排名 → 演进候选进 `candidates/`（附账本证据）→ 用户裁决 → 以 PR 提给 AscendBot → 按 `canaries/` 纪律验证 → 合入并登记版本锚点。

## 服务闭环（Codex CLI + Day0）

```bash
cd /Users/cx/code/RSI/rsi
python3 -m rsi_service serve --config examples/circle/demo.json
python3 -m rsi_service status --config examples/circle/demo.json
```

上述配置运行本地 fixture，不调用 Codex 或 NPU。真实接入使用 `examples/circle/codex-day0.example.json`，填入目标仓、Day0 skill、任务和独立检查器。服务将长验证交给独立 worker，支持重启接管、快速检查、baseline 缓存、预算和显式取消；验证通过后下一轮使用管理中的新 skill 快照。

详见 [服务说明](docs/SERVICE.md) 与 [任务清单](TASKS.md)。外部 skill 仓库的 PR 发布和真实收益归因仍保留原有证据纪律。

## 任务集与历史候选搜索

可选的 `optimizer.strategy: "pareto"` 在服务上增加训练/验证/测试任务集、结构化反思反馈、历史候选池与逐样例优势选择。搜索中的版本与实际生效版本分别管理；搜索结束后才使用留出测试决定最终切换。验证预算按任务数和完整验证批次分别限制。

```bash
python3 -m rsi_service serve --config examples/search/demo.json
```

这是参考 GEPA/gskill 机制的本地实现，不依赖上游 GEPA 包。演示仍是离线 fixture；真实 Day0 接入方式与算法边界见 [搜索说明](docs/SEARCH.md)。

## 快速跑通（复盘的机械部分）

```bash
bash tests/test_flow.sh                # 先验环境，应输出 E2E PASS
python3 scripts/seed_signatures.py <AscendBot>/workflow/skills/day0-inference/reference/troubleshooting-signatures.md
python3 scripts/import_run.py <目标仓>/.day0/<run_dir>
cp <目标仓>/.day0/<run_dir>/run_metrics.json runs/<run_id>.json
python3 scripts/count_signatures.py --registry signatures.json \
  --logs <目标仓>/.day0/<run_dir> --update-metrics runs/<run_id>.json
python3 scripts/update_ledger.py --write
python3 scripts/new_candidate.py --slug <slug> --title "<一句话>" \
  --source-run <run_id> --signature SIG-0NN
python3 scripts/check_repo.py
```

裁决之后（改造、验证、合入、对账）走 RUNBOOK §3–§5：在本仓开 Claude Code 会话，说「复盘 <run 目录>」由 `rsi-retro` 接手，说「执行 C-0xx」由 `rsi-refine` 接手。

## 目录

| 路径 | 职责 |
|---|---|
| `rsi_service/` | 持久化调度器、Codex/命令适配器与长任务 worker |
| `examples/circle/` | 离线演示、Codex/Day0 配置模板与 systemd 示例 |
| `examples/search/` | 任务集搜索演示、分区样例与 Day0 配置模板 |
| `docs/SERVICE.md` | 服务启动、长验证恢复、结果和独立验证契约 |
| `docs/SEARCH.md` | 历史候选搜索、训练反馈、评估预算与最终留出测试 |
| `TASKS.md` | 本版交付与真实 Day0 验收清单 |
| `CONTRACT.md` | skill 侧必须实现的两个稳定契约（metrics 上报、候选登记格式） |
| `runs/` | 每 run 一份 metrics 快照，只增不改 |
| `ledger/` | 跨 run 账本：失败模式频次 × 耗时 = 成本排名，回答「改哪里」 |
| `candidates/` | 演进候选队列与生命周期管理 |
| `canaries/` | 金丝雀定义与三档验证纪律、验证记录 |
| `scripts/` | 七件套：`import_run`（入账）、`seed_signatures`（播种签名注册表）、`count_signatures`（签名清点）、`update_ledger`（账本自动统计节）、`new_candidate`（建候选）、`check_signature`（轻档门）、`check_repo`（全仓自检） |
| `signatures.json` | 签名注册表（已从 AscendBot 签名库播种；`needs_review` 条目须人工补正则） |
| `.claude/skills/` | 两个驱动：`rsi-retro`（复盘会话）、`rsi-refine`（改造会话） |
| `tests/` | e2e 自检：`bash tests/test_flow.sh`，覆盖 播种→入账→清点→账本→候选→自检 全链 |

## 操作纪律

1. **锁定区**：门禁脚本（`check_*`）、G 判据定义、精度基线、签名库表结构属于「裁判」，永不进入 agent 可改范围。本仓演进的是知识库条目、判定详表、签名条目（经裁决）这类「选手件」。
2. **回算与实测分开记**：凡记录工时重排/类比得出的耗时一律标「回算」，只有重放/重跑得出的标「实测」。混记一次，账本失信一次。
3. **先薄后厚**：现有目录够用之前不新增子系统；新能力先在现有文件里以条目形式长。
4. **样本量现实**：单 run 涨跌不可归因；优先级判定要求同模式样本 ≥3，或单次成本极高。
5. **skill 独立可跑**：本仓的任何变更不得让 AscendBot 产生对 lab 的运行时依赖。
