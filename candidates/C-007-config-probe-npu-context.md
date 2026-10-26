# C-007：启动 owner 中的配置解析可创建 NPU 上下文

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：serve-lifecycle.md / golden-service-knowledge.md（知识库条目）。

## 问题

实证同 PID import 后 torch_npu initialized=false，EngineArgs.create_engine_config 后 true；held-context 原 helper preflight exit2/PROCESS_TABLE_UNKNOWN。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：会查询设备的 config/identity probe 放独立有限 helper receipt，确认退出/RELEASED 后由 stdlib owner 启动真实服务。**候选已批准但真实 B 回归待验**。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

serve-lifecycle.md / golden-service-knowledge.md（知识库条目）。

## 验证计划

- 档位：轻：文档并入；真实 B 回归挂账到下个 Day0
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
