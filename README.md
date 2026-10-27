# RSI Lab — skill 演进外环

> **当前状态（诚实口径）**：基础设施已建成并通过自检；**「改进有效」尚未验证**——还没有走完一次"改 skill → 对照 → 账本记结论"的闭环。第一次闭环以 canaries 验证记录 + 账本「改动收益」行为准，在此之前请把本仓当作"有纪律的复盘基础设施"，不要当作已验证有效的 RSI 系统。

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
