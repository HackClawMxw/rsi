# C-018：S4 已达标后的精简出口

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：C（C=判据/流程边界)
- 影响面：**流程行为边界候选**：Stage4 flow 尚部分接入，改 flow 执行计划=动流程件；建议随 performance_flow 完整接入时一并设计，本批暂缓。

## 问题

默认 S4.1 基准已 9.41ms 远低于 40ms，flow 仍要求 profiling/定向调优/最终复测；INFO 试验仅改善 0.039% 且 P95 回退。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：正确性已签且有重复测量余量时，允许保留最终复测而跳过低收益调优。profile 窗口 16m24.8s 属混合观察区间，不能全记 skill 净浪费。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

**流程行为边界候选**：Stage4 flow 尚部分接入，改 flow 执行计划=动流程件；建议随 performance_flow 完整接入时一并设计，本批暂缓。

## 验证计划

- 档位：中：下个真实 Day0 的 S4 段实测
- 通过判据：边界类：用户逐条裁决后另立验证计划

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
