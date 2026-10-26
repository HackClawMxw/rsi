# C-015：有限退出后的显存尾部复查

- 状态：evidence-attached
- 来源：run qwen38_20261024_1 / 契约二登记（self-evolving.md 转录）
- 关联签名：none
- 分组：A（A=知识库并入)
- 影响面：serve-lifecycle.md / dummy-test.md 知识条目。

## 问题

固定 helper 在进程轮询后仅单次设备快照；两次首检到最终释放观察区间共 149.838s，最终 signals[]。

## 证据

原文摘录（/Users/cx/Desktop/code/agent/sessions/qwen38-20261024-1/evidence/self-evolving.md）：
> 原条目：身份/阈值不变前提下有限重采 HBM，不把观察区间等同实际释放时延。证据 lifecycle-friction-timing.json（实测）。

量化数据均为原条目实测口径；完整原文见来源文件对应条目。

## 建议改动

serve-lifecycle.md / dummy-test.md 知识条目。

## 验证计划

- 档位：轻：文档并入
- 通过判据：文档类：check_repo 通过 + 下个 Day0 对应段无同类摩擦登记

## 裁决与结果

| 日期 | 动作 | 备注 |
|---|---|---|
