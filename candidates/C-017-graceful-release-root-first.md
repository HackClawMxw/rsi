# C-017：C3 standard root-first graceful release

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：troubleshooting-signatures（新增签名条目候选）+ serve-lifecycle 知识。

## 问题

标准 MP 服务子进程存活且同父子链，但 /proc 运行标记为空；原停止工具发信号前 UNKNOWN。root 严格验证后 TERM 一次，3.339s 无 live，三项 RELEASED。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：既有工具提供严格验证原 root 的 graceful-first 路径，再核三项释放；不向未知子进程发信号。首次 UNKNOWN→fresh0 共 169.6s（实测，非纯释放延迟）。已实测标记缺失，环境重写原因未确证唯一根因。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

troubleshooting-signatures（新增签名条目候选）+ serve-lifecycle 知识。

## 验证计划

- 档位：轻：签名入库走 check_signature 回放（须取到原始日志，服务器侧补）
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
