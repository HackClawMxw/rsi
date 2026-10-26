# C-003：Worker 前后 KV 物理规格分层

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：golden-worker-knowledge.md / designer 判定详表（知识库）。

## 问题

纯 EngineArgs/planner 未经历平台 update_block_size_for_backend，误将 block16/page400896 当终态误判长上下文不可装；实际 TP8 Worker block512/Mamba padded524288。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：设计须标注 pre-worker 与 post-worker 证据层级，容量计算前必须覆盖平台对齐链。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

golden-worker-knowledge.md / designer 判定详表（知识库）。

## 验证计划

- 档位：轻：文档并入 + 下个真实 Day0 的 S1 段验证
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
