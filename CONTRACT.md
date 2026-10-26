# skill 侧契约（AscendBot ↔ lab 的两个稳定接口）

> 接口变更走双向评审：本仓改 schema 必须升 minor 并在文末登记；AscendBot 侧实现以本文为唯一依据。

## 契约一：closeout 上报 run_metrics.json

**谁做**：skill 的 Run 终止协议（关门清单）在写 `closeout.md` 的同一步产出本文件。
**落点**：目标仓 `.day0/<run>/run_metrics.json`。
**导入**：lab 用 `scripts/import_run.py <run_dir>` 转录进 `runs/`（导入失败不打扰 run 本身）。契约生效前的历史 run，也用该脚本从事后挖账。

```json
{
  "run_id": "qwen3_8_20261024_1",
  "skill_anchor": "ascendbot@a2eabdd",
  "model": "Qwen3.8",
  "stages": {
    "S1": {"status": "signed", "seconds": null},
    "S2": {"status": "signed", "seconds": null},
    "S3": {"status": "signed", "seconds": null},
    "S4": {"status": "signed", "seconds": null}
  },
  "total_span_seconds": null,
  "human_wait_seconds": null,
  "authorization_windows_seconds": null,
  "signature_hits": {"SIG-011": 1, "SIG-040": 2},
  "rework_rounds": 1,
  "friction_count": 2,
  "outcome": "signed",
  "source_tracker": ".day0/qwen3_8_20261024_1/tracker.md",
  "notes": ""
}
```

字段纪律：

- **取不到的值一律 `null`，禁止估、禁止编**。`null` 是信息（没测），不是缺陷。
- `skill_anchor`：AscendBot 方法论 git 短锚点（`git rev-parse --short HEAD`），涨分归因的坐标。
- `stages.*.seconds`：该阶段首末事件间隔；跨阶段重叠不扣，账本只做保守统计。
- `human_wait_seconds` / `authorization_windows_seconds`：优先由 tracker 等待事件归集；无把握则 `null` 留人工标注。
- `outcome` ∈ `signed | pending | aborted`，对齐 tracker「run 终态」枚举。
- `stages.*.status` ∈ `signed | fail | pending | skipped | na`，对齐 tracker 终态盘口的阶段状态。

## 契约二：演进候选机读登记

step 6 自演进咨询保持人读的 `self-evolving.md` 不变，但每条候选追加一行机读索引（lab 只转录、不发明）：

```
<!-- LAB: {id} | {source_run_id} | {signature|none} | {场景短语} -->
```

- 一条一行，紧跟对应条目；字段内不得出现 `|`（用 `；` 代替）。
- `signature` 为关联合签名 ID，无关联填 `none`。
- lab 侧 `candidates/C-<seq>-<slug>.md` 由此转录，须原文引用来源 run。

## 版本记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-26 | 0.1 | 初版：契约一（run_metrics）、契约二（候选登记） |
