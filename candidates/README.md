# candidates/ — 演进候选队列

裁决在 lab 完成——离开 23 小时 run 的尾声，人不必在收工时做决定。候选来源：契约二登记转录 + 账本高优先级项反推。

## 生命周期

```
registered → evidence-attached → adjudicated(批准/暂缓/拒绝)
           → change-proposed(PR#) → canary-passed → merged(<版本锚点>)
```

状态只允许沿箭头走；暂缓/拒绝的候选保留在原文件，不删（负样本也是账本的一部分）。

## 命名与模板

`C-<三位序号>-<slug>.md`，从 `template.md` 复制。

## 裁决纪律

- 呈报必附三样：账本对应行、影响面（改 AscendBot 哪些文件）、验证计划（三档选哪档）。
- 同类改动连续三批零打回 → 可提名「预审类」（自动并入 + 事后抽查）；提名本身单独成候选，由用户批。
