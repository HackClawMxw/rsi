# RSI Lab — skill 演进外环

> **当前状态**：提供离线复盘工具与可启动的 RSI circle 服务。服务支持 Codex CLI → 候选 skill → 独立验证 → 下一轮调度；真实 Day0 的改进效果尚待生产 canary 和同锚点多 run 对账验证，离线演示不能替代这些证据。

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
