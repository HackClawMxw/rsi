# C-005：观测入口与控制进程拓扑分层

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：parallel_design.md / designer 文档（知识库）。

## 问题

Stage2 全量 KV 视图可经 worker 字符串 RPC 采集，但 native coordinator 在 EngineCore 进程，worker RPC 不能替代；HTTP 开发端点存在不证明标准服务已启用。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：设计时显式分别声明 worker/coordinator 入口与实际可调用性，缺口 fail-closed。依据 parallel/design/change-CR-S2-observation-01.md。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

parallel_design.md / designer 文档（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
