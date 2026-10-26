# C-010：封存证据的相对基准和字段契约

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：封存/索引规范（知识库）。

## 问题

不同 index 分别以 case 目录/run 根/绝对路径为基准；root 只读工具曾因错误基准失败；index_sha256 与 release_index_sha256 不一致引发离线 KeyError。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：索引显式声明 base_path 及版本化 schema，消费者先读 schema 并校验字段。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

封存/索引规范（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
