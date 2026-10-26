# C-013：Prefix retention 配置字段与平台实际消费者分层

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：feature-knowledge / kv/版本语义知识条目（含已 head 部分的 Prefix retention 默认 0 版本语义候选一并归并）。

## 问题

Ascend coordinator 从旧 env 读 retention，CLI/CacheConfig 512 不等于内部已透传（实际 None）；仅 physical/hash/hit512、checkpoint0、无 MTP/connector 时 None 与 512 同 dense，0 不同。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：同字段不同层次分开取证，按当前消费者逐项条件等价裁决，不修改环境制造透传表象。证据 B2-retention-root-ruling.json。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

feature-knowledge / kv/版本语义知识条目（含已 head 部分的 Prefix retention 默认 0 版本语义候选一并归并）。

## 验证计划

- 档位：轻：文档并入；full64 实际条件挂账下个 Day0
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
