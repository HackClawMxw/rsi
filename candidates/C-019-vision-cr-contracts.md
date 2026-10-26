# C-019：当前视觉 CR 及服务终验的契约补充（四合一）

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：feature-knowledge / 视觉特性知识（知识库）。

## 问题

①视觉归一化单一所有者（覆盖 missing-layer/meta/empty-MM 及真实权重 A-B-A）；②输出预算须在测试计划前声明（默认 128 截断不能硬改 stop 通过）；③固定模板消费者按 chat_template trim 规范化语义比较；④CPU 解码可用 Rust backend 并逐字验证等价。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：四条均为本 run 实证，observer 错误属执行方，不计 Skill 耗时；补验以实际后续 root gate 为准，本条不预签。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

feature-knowledge / 视觉特性知识（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
