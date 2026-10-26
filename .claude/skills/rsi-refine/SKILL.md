---
name: rsi-refine
description: "RSI lab 改造会话：按已批准的候选修改 AscendBot 方法论并提 PR，过轻档门（签名回放校验），登记验证计划。触发词：执行候选、改造 skill、提 PR。纪律：锁定区碰到即停；无验证记录不合入。"
---

# RSI 改造会话

针对一个已批准（`adjudicated` 且结论为批准）的候选 `C-*.md` 执行。

## 固定动作

### 1. 影响面核对

读候选的「影响面」列出的 AscendBot 文件清单。**锁定区碰到即停并向用户上报**：
门禁脚本（`check_*.py`）、G 判据定义、精度基线、签名库表结构。
允许改的是：知识库条目、designer 判定详表、签名条目内容、flow 的非门禁环节。

### 2. 实施

- 在 AscendBot 仓按候选「建议改动」实施；改动前后记录 `git rev-parse --short HEAD`。
- 改动范围严格限于影响面清单；发现需要越界时停下呈报，不自行扩权。

### 3. 轻档门（涉及签名条目时必过）

```bash
python3 scripts/check_signature.py --pattern '<签名正则>' \
  --logs <该签名声称来源的历史日志>
```

退出码非 0 = 未命中 = 停，向用户呈报证据，不进入提 PR。

### 4. 提 PR

PR 描述必含：候选文件路径、验证计划（档位/段落/判据）、skill 锚点（改动前）。
**不自行合并。**

### 5. 状态推进

候选文件状态行改为 `change-proposed`，并在「裁决与结果」表追加一行（日期、动作、PR 号）。

## 后续（不在本会话完成）

- 中/重档验证按 `canaries/README.md` 在目标仓执行，结果登记 `validated-versions.md`——**无记录不合入**。
- 用户合入后：状态改 `merged(<新锚点>)`；该锚点起 run_metrics 携带新 `skill_anchor`，≥3 个 run 后跑对账（RUNBOOK §5）。

## 禁止

- 不得碰锁定区。
- 不得跳过轻档门。
- 不得自行合并 PR 或翻转 `canary-passed`/`merged` 状态。
