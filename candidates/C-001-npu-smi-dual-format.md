# C-001：npu-smi 26.1.1 A3 双芯格式解析失败

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：ops-analysis 的 preflight/bootstrap 解析知识（知识库条目，非脚本）。

## 问题

技能原 npu-smi 解析器拒绝真实健康设备（新格式新增 Chip Phy-ID 映射列、按板卡声明无进程）。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：本 run 仅修远端技能副本，原件、diff、30 项回归均归档 preflight/bootstrap/。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

ops-analysis 的 preflight/bootstrap 解析知识（知识库条目，非脚本）。

## 验证计划

- 档位：轻：文档并入 + check_repo；若涉解析脚本须单独授权
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
