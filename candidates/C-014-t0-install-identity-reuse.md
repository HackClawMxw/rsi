# C-014：重复 T0 安装的身份复用

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：execution-reuse.md（知识库）。

## 问题

F1 三轮 T0 安装 113/121/121 秒，后两轮共 242 秒八类环境身份相同。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：安装证据按代码/native/toolchain 身份复用，设备卫生与锚点核验继续执行；不把 355 秒全称编译。证据 timing-evidence.json（实测）。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

execution-reuse.md（知识库）。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
