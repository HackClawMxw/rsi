# C-012：平台注册链的实际类身份与 CPU 证据层级

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：golden-worker-knowledge.md / 测试层级知识（知识库）。

## 问题

F2 初批 CPU manager 测试未执行 Ascend 平台 patch 与 register_all_kvcache_specs，实际是 upstream manager，不能作为 Ascend 生产 dispatch 覆盖。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：先经当前平台真实注册入口，记录 module/source/MRO 及 registry 解析；CPU fixture 边界显式声明。证据 offline-manifest.json SHA3ef74…。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

golden-worker-knowledge.md / 测试层级知识（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
