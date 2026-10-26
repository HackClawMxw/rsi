# C-008：Mixed per-case 用户判据与 exact 锚点共存

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：C（C=判据/流程边界)
- 影响面：**判据域边界候选**：工具层原生支持=官方验证器域（锁定区）。本 run 的 sidecar 做法可先入知识库，工具改造另议。

## 问题

同四题回归：一题允许推理 token 差异只看最终答案，三题仍需 raw exact；官方 emit_baseline 只接受全 raw 自洽。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：显式逐题判据 + 外层完整题集契约；本 run 用官方三题锚点+第 4 题自定义 sidecar、SHA 交叉绑定，原 FAIL 不改。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

**判据域边界候选**：工具层原生支持=官方验证器域（锁定区）。本 run 的 sidecar 做法可先入知识库，工具改造另议。

## 验证计划

- 档位：轻（仅知识库部分）；工具部分重档另议
- 通过判据：边界类：用户逐条裁决后另立验证计划

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
