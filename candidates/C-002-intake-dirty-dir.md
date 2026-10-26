# C-002：立项预检被自身产物 .pending_params.json 判脏

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：B（B=触碰脚本锁定区)
- 影响面：立项校验器（check_*/init 脚本）

## 问题

立项预检失败已写 .day0/.pending_params.json，重试被自身生成目录报脏。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：本 run 通过 .git/info/exclude 排除 .day0/，代码树未改动。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

立项校验器（check_*/init 脚本）——**触碰锁定区，须用户单独授权后动脚本**。

## 验证计划

- 档位：中：临时目录构造重试场景试跑校验器
- 通过判据：脚本类：临时目录用例试跑 + check_repo

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
