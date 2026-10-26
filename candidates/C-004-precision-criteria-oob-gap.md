# C-004：精度默认判据与 OOB 承载缺口

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：C（C=判据/流程边界)
- 影响面：**判据域边界候选**：accuracy/G3 判据定义属锁定区；跨仓（RCABot）议题，建议单独立项讨论而非本批并入。

## 问题

Day0 默认 teacher-forcing top1/cos 判据，但 OOB 只实现自洽/token exact；多模态 image_url 不被默认 collect_outputs 透传。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：跨仓讨论有溯源的 logits 独立证据契约，保留旧 FAIL 与采样协议，不以 schema 通过冒充判据通过。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

**判据域边界候选**：accuracy/G3 判据定义属锁定区；跨仓（RCABot）议题，建议单独立项讨论而非本批并入。

## 验证计划

- 档位：重：跨仓方案讨论 + 判据变更须用户逐条批准
- 通过判据：边界类：用户逐条裁决后另立验证计划

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
