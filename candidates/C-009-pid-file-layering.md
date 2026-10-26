# C-009：封存索引与生命周期 PID 文件分层

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：serve-lifecycle.md / 封存规范（知识库）。

## 问题

F1 C0 handoff-index.json 封存 77 文件，其中 serve.pid 由生命周期 helper 正常 stop 时删除；释放后全量存在性检查 FileNotFoundError，其余 76 文件 hash 保持。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：封存不可变 PID 身份 snapshot，活 PIDfile 作为生命周期引用单列；stop 后以新 release 索引关联。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

serve-lifecycle.md / 封存规范（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
