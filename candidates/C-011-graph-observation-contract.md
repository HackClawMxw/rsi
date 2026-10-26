# C-011：图模式观测的计数与拓扑契约

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：performance.md / feature-flow 观测素材（知识库，非门禁件）。

## 问题

原诊断只有 aggregate mixed 信息，不能证明同一步 prefill/decode 共存；dummy PW capture15、full64 PW195、FDO3 与实际 startup forward7 是不同量。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：前置声明 host-only 有界观测、四个互斥 startup 分类、同 worker step 的 CPU rows 与 scheduled IDs 关联。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

performance.md / feature-flow 观测素材（知识库，非门禁件）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
