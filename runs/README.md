# runs/ — 每 run 一份快照

## 命名

`<model-slug>_<yyyymmdd>_<n>.json`，与 `run_id` 一致。

## 导入

```bash
python3 scripts/import_run.py /path/to/vllm-ascend/.day0/<run_dir>
# 产出 <run_dir>/run_metrics.json，转录为 runs/<run_id>.json
```

## 纪律

- **只增不改**：已入仓的快照是历史证据，修正以顶层 `"amendments": [...]` 追加（注明日期与理由），禁止原地改数。
- 每份快照必须能回指原始 tracker（`source_tracker` 字段，import 脚本自动填）。
- 快照之间不可比时（版本锚点不同、环境大改），在 `notes` 里写明，账本聚合时分层。
