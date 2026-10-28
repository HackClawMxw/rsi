# RUNBOOK — 一轮完整循环的操作手册

> 本仓的「skill」。目录骨架是静态的，本手册是让文件动起来的程序。
> 本手册保留人工复盘与外部 PR 发布流程。自动执行和耗时验证由新增的 [circle 服务](docs/SERVICE.md) 承担；两种入口遵循同一证据口径。

## 0. 全景

```
        用户（新模型需求）
             │ ① 跑 run（AscendBot skill，在目标仓）
             ▼
   .day0/<run>/：tracker、门禁证据、closeout、run_metrics.json、摩擦登记
             │ ② 入账（复盘会话）
             ▼
   runs/<run_id>.json + candidates/（registered）+ ledger 更新
             │ ③ 归因成案（复盘会话）
             ▼
   候选 evidence-attached（证据 / 影响面 / 验证计划）
             │ ④ 批量裁决（人）
             ▼
   批准 / 暂缓 / 拒绝
             │ ⑤ 改造（改造会话）      ⑥ 金丝雀验证      ⑦ 合入（人）
             ▼
   AscendBot PR → validated-versions.md → merged(<skill_anchor>)
             │ ⑧ 对账（≥3 个同锚点 run 后，复盘会话）
             └──→ 账本「改动收益」行 ──→ 决定下一轮改哪里（回到 ①）
```

**人只出现在 ①④⑦ 三个点**：发起 run、批量裁决、最终合入。其余全部由会话执行。

## 1. 阶段表

| # | 阶段 | 触发 | 操作者 | 输入 | 输出与落点 | 判完成 |
|---|---|---|---|---|---|---|
| ① | 跑 run | 用户新模型需求 | run-agent（skill 驱动） | 立项参数 | `.day0/<run>/` 全套 + run_metrics.json + 摩擦登记 | tracker 终态 |
| ② | 入账 | run 终止后，用户一句话 | 复盘会话 | run 目录 | `runs/<run_id>.json`；LAB 行转录为候选；ledger 重算 | 快照入仓、候选 registered |
| ③ | 归因成案 | 复盘会话续 | 复盘会话 | ledger 排名 + 候选 | 候选补全证据/影响面/验证计划 | evidence-attached |
| ④ | 批量裁决 | 候选齐批 | **人** | 按优先级排序的裁决单 | 状态翻转（批准/暂缓/拒绝） | 全部有裁决 |
| ⑤ | 改造 | 候选批准 | 改造会话 | 候选文件 | AscendBot PR（过轻档门） | PR 可评审 |
| ⑥ | 验证 | PR 前后 | 改造会话 + 目标仓机器 | 验证计划 | `validated-versions.md` 新行 | 有记录 |
| ⑦ | 合入 | 验证通过 | **人** | 验证记录 | merge；候选置 merged(锚点) | 锚点前进 |
| ⑧ | 对账 | 新锚点积累 ≥3 run | 复盘会话 | 分锚点的 run_metrics | 账本「改动收益」行 | 有结论（有效/无效/未证实） |

## 2. 标准复盘会话

run 跑完后，在 lab 目录用一句话拉起：

> 复盘：导入 `<目标仓>/.day0/<run_dir>`，更新账本，起草候选，呈报裁决单。

会话的固定动作（按序，缺输入就停下来要，不猜）：

1. `import_run.py <run_dir>` 入账 → `runs/`；解析不了的行如实呈报，不修数。
2. 转录 `self-evolving.md` 的 `<!-- LAB: ... -->` 行 → `candidates/C-xxx.md`（registered）。
3. 更新 `ledger/`：新 run 并入频次/耗时统计；重算成本排名。
4. 对 registered 候选逐条补证据（tracker 时间账、日志、账本行）、影响面、验证计划 → evidence-attached。
5. 产出裁决单呈报：候选 × 账本优先级 × 建议动作，等人。

**纪律**：复盘会话只呈报，不裁决、不并入、不碰 AscendBot。

## 3. 改造会话

> 执行 C-0xx：按其建议改动改 AscendBot，提 PR。

固定动作：

1. 只碰候选「影响面」列出的文件；**锁定区**（门禁脚本、G 判据、精度基线、签名库表结构）碰到即停，上报。
2. 涉及新签名：`check_signature.py --pattern <正则> --logs <声称来源的日志>` 过轻档门，未命中即停。
3. PR 描述引用候选文件路径、验证计划、skill_anchor。

## 4. 验证与合入

- 中/重档按 `canaries/README.md` 执行；结果登记 `validated-versions.md`。**无记录不合入。**
- 合入后：候选置 `merged(<锚点>)`；下一个 run 的 run_metrics 起携带新 `skill_anchor`——这是第 ⑧ 步对账的前提。

## 5. 对账（回答"上一轮改动有没有用"）

新锚点积累 ≥3 个 run 后，复盘会话按 `skill_anchor` 分层对比：

- 同签名/同模式的频次与耗时，改动前 vs 改动后；
- 账本追加「改动收益」行：候选 × 指标变化 × 结论（**有效 / 无效 / 未证实**）；
- 无效改动回写候选（负样本保留），账本优先级随之修正。

**没有第 ⑧ 步，循环只是在原地转**——改了、跑了、但从不学习"改动是否有效"。

## 6. 服务化入口

按新的服务化需求，自动执行、候选改造、独立 canary 调度和下一轮版本选择由 `rsi_service` 实现，操作见 [docs/SERVICE.md](docs/SERVICE.md)。先运行本地 demo；真实 Day0 配置须包含目标仓、skill、完整任务和独立检查器。

完整验证先于版本提升。长任务在独立 worker 中执行，调度器重启后继续监听；已有 baseline 只在内容、实验上下文及 TTL 一致时复用。没有验证结果时不切换 skill，也不把 CLI 退出 0 当成 Day0 验收通过。

服务使用自己的实验状态和 skill 快照，不自动改写外部 skill 仓库。外部 PR 的合入仍按 §4；真实收益是否有效仍按 §5 的同锚点多 run 证据判断。
