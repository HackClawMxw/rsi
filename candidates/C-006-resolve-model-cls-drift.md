# C-006：启动检查器 resolve_model_cls 接口漂移

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：B（B=触碰脚本锁定区)
- 影响面：启动检查器脚本

## 问题

本树 resolve_model_cls 要求 architectures 和 model_config，旧 checker 仅传 architectures，C3 SKIP/exit2；重复跑 checker 不能补齐接口。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：checker 依据安装版签名适配调用并验证实际 resolved class，保留未适配时原退出码与人工核销证据。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

启动检查器脚本——**锁定区**。候选只提适配原则，改动须用户逐条授权。

## 验证计划

- 档位：中：临时目录用新旧两种 vLLM 签名试跑
- 通过判据：脚本类：临时目录用例试跑 + check_repo

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
