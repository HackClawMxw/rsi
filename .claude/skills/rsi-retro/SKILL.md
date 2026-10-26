---
name: rsi-retro
description: "RSI lab 复盘会话：把一个 run 的产物导入本仓，重算账本，起草/补全演进候选，产出裁决单呈报用户。触发词：复盘、导入 run、更新账本、候选起草、裁决单。纪律：只呈报，不裁决，不碰 AscendBot。"
---

# RSI 复盘会话

你按固定动作执行复盘，顺序不可乱，缺输入就停下来向用户要，**不猜**。

## 前置

- 用户给出 run 目录（目标仓 `.day0/<run>/`）或指名要补全哪些候选。
- 所有命令在本仓根目录执行；脚本输出如实转述，解析失败/跳过行数不许隐瞒。

## 固定动作

### 1. 入账

```bash
python3 scripts/import_run.py <run_dir>
cp <run_dir>/run_metrics.json runs/<run_id>.json
```

- 若 extraction 里 `events_skipped > 0`，在呈报中列出未解析的原始行。
- `runs/<run_id>.json` 已存在时停止，向用户确认（只增不改纪律，修正走 amendments）。

### 2. 转录候选（用户给了 self-evolving.md 时）

对其中每行 `<!-- LAB: id | run_id | sig | 场景 -->`：

```bash
python3 scripts/new_candidate.py --slug <从场景短语取英文短横线串> \
  --title "<场景短语>" --source-run <run_id> --signature <sig|none>
```

新建的候选保持 `registered` 状态。

### 3. 签名清点 + 账本重算

```bash
python3 scripts/count_signatures.py --registry signatures.json \
  --logs <run_dir> --update-metrics runs/<run_id>.json
python3 scripts/update_ledger.py --write
```

### 4. 补全候选（对每个 registered 候选）

用 Edit 修改 `candidates/C-*.md`：

- 「证据」：tracker 时间账（标实测）、相关日志摘录、ledger 对应行；**回算类证据必须标「回算」**。
- 「影响面」：拟改 AscendBot 哪些文件；确认不碰锁定区（门禁脚本、G 判据、精度基线、签名库表结构）。
- 「验证计划」：三档选一（轻=签名回放 / 中=金丝雀段落 / 重=升版全段），写明段落与通过判据。
- 状态行改为 `evidence-attached`。

## 收尾

```bash
python3 scripts/check_repo.py
```

自检不过先修（缺字段补 null 不算过）。然后向用户呈报**裁决单**：每候选一行——ID、一句话、账本优先级依据（频次×耗时）、建议动作（批准/暂缓/拒绝）、验证档位。**等用户裁决，不得自行翻转状态。**

## 禁止

- 不得修改 AscendBot 仓任何文件。
- 不得替用户裁决候选。
- metrics 里取不到的字段一律留 null，禁止估算。
