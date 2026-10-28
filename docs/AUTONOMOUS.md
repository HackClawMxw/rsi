# 从原始 Codex session 开始的自主循环

`autonomy` 模式把原始执行记录接入持续运行的服务：分析证据、提出假设、修改候选 skill、执行基线与候选、独立检查、判定、反思，再用新执行记录开始下一轮。它使用内置 Codex 角色，不要求先写一个模拟 LLM 的外部 harness。

它与已有 `optimizer` 模式互斥。自主循环当前面向**同一个固定任务的成对回放**；这不是训练集、验证集、保留测试集上的泛化评估，也没有把一次成功回放当成普遍有效的 skill 改进。

## 先跑一个全 CPU 例子

仓库中的 [examples/autonomous/config.json](../examples/autonomous/config.json) 已配置可执行示例。需要 Python 3.9+、可执行的 Codex CLI，以及用户已有的 Codex 登录/模型配置。从仓库根目录运行：

```sh
python3 -m rsi_service serve --config examples/autonomous/config.json
```

配置没有 `steps`，因此 `analyze / plan / improve / run / judge / reflect` 六个角色使用真实 `codex exec`。`quick` 与 `checks` 是服务执行的本地确定性命令。示例不会访问 NPU、远程服务器、安装软件或运行网络业务；Codex 本身仍需访问其模型服务并消耗对应额度。

示例包含：

- `source-session.jsonl`：8 条公开合成原始事件，记录窄签名 observer 遇到 `loaded_weight` 关键字后失败及其恢复过程。它不包含真实用户的私有 session。
- `skill/SKILL.md`：不可修改的执行约束；`skill/notes.md`：允许演化的旧 observer 配方。
- `target/protocol.py`：四个固定 CPU 行为用例，验证位置参数、关键字参数、返回对象身份和原异常身份。运行者生成 `observer.py` 与实际 `report.json`。
- `config.json`：固定目标、预算，以及内嵌的 Python operator check。该检查直接执行回调行为，并校验 `protocol.py` 的内容哈希，不能通过修改工作区中的测试脚本把失败改成通过。

初始 recipe 的 `(param, weight, **kwargs)` 包装不能透明转发 `loaded_weight=`。改进假设可以是修改 notes，让运行者按实际回调协议实现透明转发，减少无效失败和重试。示例不保证模型一定采用这个假设或产生正收益：基线可能自行提前修复，候选也可能无效。**完整执行与诚实拒绝同样是有效的 smoke 结果；只有固定规则满足时才晋升。**

默认最多 1 个 cycle、2 次验证 run、12 次模型角色作业和 30 分钟总墙钟时间。每次 `run` 最长 300 秒。默认状态目录为 `/tmp/rsi-autonomous-cpu-smoke-v1`，在 macOS 上会解析为 `/private/tmp/...`。

状态目录是实验身份的一部分。重启同一配置会续接原实验，不会清空后重跑。要建立新实验，在**首次启动前**复制整个示例目录并给副本配置设置新的 `state_dir`；不要修改已经初始化的配置或旧实验产物来制造新实验。首次配置设成 2 个 cycle 时，将 `max_validation_runs` 至少设为 4，并为新 session 的分析留足 `max_model_calls`；模型也可能因没有合理改进而提前停止。

## 任务、目标和证据分别是什么

顶层 `task` 是每次真实验证要执行的任务，包括允许修改的产品文件、环境操作和实际验证范围。`objective` 则是 operator 固定的比较合同，不由 plan 或 improve 角色重新定义：

```json
{
  "objective": {
    "description": "减少观察器引起的无效失败，保持固定协议正确性",
    "gates": [
      {"id": "protocol_correct", "description": "四个固定协议用例均有实际通过证据"}
    ],
    "metrics": [
      {
        "name": "failed_commands",
        "description": "仅统计本次新执行session中worker识别到的失败工具调用",
        "direction": "minimize",
        "source": "failed_tools",
        "min_gain": 1,
        "max_regression": 0,
        "weight": 1
      }
    ]
  }
}
```

至少一个 gate 和一个 metric 是必需的。`description` 应写明适用范围，指标的可选 `description` 应说明单位、计分规则与缺证据时如何处理。`direction` 是 `minimize` 或 `maximize`；`min_gain`、`max_regression` 使用指标本身的单位。多指标权重负责缩放，服务不会自动把秒数、分数和次数归一化。

指标 `source` 可以是 `judge`、`wall_seconds`、`tool_calls`、`failed_tools`。机器指标由 worker/原始事件计算后覆盖模型声称的值，并标记 `provenance=worker`、`evidence_refs=["worker.stats.<来源>"]`；原模型引用留在 `judge_evidence_refs`。其中 `wall_seconds` 是整个验证角色作业的墙钟时间，包含模型推理、工具执行及等待，**不是纯模型服务延迟、NPU 时间或资源排队时间的分解**。

`source=judge` 的指标是模型基于实际证据按 operator rubric 作出的评价。没有预先规定的明确尺度时，不应将它当成严格数值测量。示例使用机器失败次数作比较指标，并加固定机械行为检查。

`allowed_paths` 只授权候选 skill 内可修改的相对路径。`SKILL.md`、`scripts/**`、`check_*` 和 `**/check_*` 默认受保护。目标、配置、判断脚本和基线不会因为模型提出新方案而变成可编辑文件。

## 导入原始 session，而不只导入总结

使用明确路径：

```json
{"autonomy": {"source": {"path": "session.jsonl", "format": "rollout"}}}
```

或者使用 session ID 和明确的查找根目录：

```json
{"autonomy": {"source": {"session_id": "your-session-id", "roots": ["/path/to/sessions"]}}}
```

路径相对于配置文件所在目录解析。`format` 支持 `auto`、`rollout` 和 `exec`；格式识别与源文件读取在 ingestion 时执行。导入会保留原始 payload、稳定事件引用和内容哈希，模型的分析、计划和判断都应引用实际事件，而不能用 `self-evolving.md` 或最终总结替代原始证据。

对于几十到上百 MB 的长期 session，默认 `analysis_view="timeline"`：完整索引覆盖每条事件，包含类型、工具/调用关联和有界预览；LLM 逐块检查索引，再按疑点读取对应完整原文。`timeline_chunk_chars` 默认 120000，`timeline_preview_chars` 默认 160，可在 0–4000 范围内设置。**覆盖所有事件的索引，不等于逐字审读全部 145 MB 原文。**

`analysis_view="full"` 要求分析完整 payload 分块；`chunk_chars` 默认 24000，原始内容仍可追溯。大文件可能需要远超默认模型作业预算的分析块。服务会先检查是否有足够预算覆盖全部分析索引/分块与计划；不足时明确报告预算限制，不会把未分析完的记录描述成完整分析。`max_session_bytes` 默认 512 MiB，是输入上限，不是保证模型预算足够的承诺。

## 每一轮怎样执行

```text
当前原始session
  → analyze（timeline或full，每个分析块）
  → plan（一个有证据的假设，或no_change/needs_input）
  → improve（只修改候选skill允许路径）
  → quick（静态差异与保护边界）
  → baseline run → operator checks → judge
  → candidate run → operator checks → judge
  → 确定性比较 → reflect → 保留版本与反例
  → 候选的新原始执行session进入下一轮
```

默认基线不复用：`autonomy.baseline_cache_ttl_seconds=0`。有候选且进入验证的完整 cycle 通常是**两次 run**，而不是每次执行后再安排一轮 deployment run。晋升只将已验证 skill 版本设为服务的 active version；不会额外重复运行验证，也不会自动发布到全局 skill 安装位置或远程模型服务。

每次新验证执行的原始 session 都会被收集、哈希并保存。候选被拒绝时，它的新 session 和反例仍可供下一轮学习；被拒绝的 skill 不会成为 active version。每轮 `learning.json` 保存假设、决策与反思。达到预算、没有变化或缺少必要输入都可能让流程在两次 run 之前结束。

## 比较和检查的边界

晋升必须同时满足：

1. 基线与候选覆盖同一固定 gate/metric 集；基线 gate 不能是 unknown。
2. 候选所有 gate 均 pass，所有比较指标都有有限数值。
3. 每个指标不超过允许回退；至少一个指标有严格正收益且达到其 `min_gain`。
4. 方向化收益的加权和大于零。
5. 候选实际执行完成、operator checks 通过，执行证据完整且未被修改。

`signed` 不是独立质量结论。缺失证据、null/NaN/Inf、unknown、失败的 operator check 或已变更证据都不能授权晋升。基线 gate 的明确 fail 可以被候选修复；unknown 代表无法可靠比较。

`autonomy.checks` 是 operator 配置的固定命令数组：

```json
{"id": "fixed_check", "argv": ["python3", "-c", "print('operator-owned check')"], "timeout_seconds": 30}
```

这段仅展示配置形状；真正的检查必须断言业务行为。服务以 argv 方式执行，不经 shell。只替换完整参数 `{workspace}`、`{skill_dir}`、`{output_dir}`、`{python}`。检查 ID 必须匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`。命令不能来自 model plan，验证尺度也不能随候选调整。模型 judge 的引用校验防止引用不存在的事件，但并不保证它的推理正确；重要门禁应有独立机器检查。空 checks 只表示“未配置独立任务检查”，不表示 Day0 机器验收通过。

## 长验证、工作区与环境身份

服务先冻结本地 workspace，再为 baseline 和 candidate 各复制一份。默认排除 `.git`、`.day0`、`__pycache__`、`.venv`、`node_modules`。这保护基线输入一致性，也意味着副本不自动具备 Git 历史、虚拟环境、node 依赖或已有 Day0 运行目录。任务需要这些内容时，应显式设计输入与环境准备，不要依赖工作区偶然存在的缓存。

**远程容器、NPU 卡、服务端口、权重挂载和数据库并不会随本地工作区复制。** 真实 Day0 接入要由 task 和 operator checks 实证绑定机器、代码/权重、容器、并行配置、资源租约、服务清理及精度口径。`autonomy.evidence_inputs` 可以列出环境指纹、负载或机器验收脚本文件，加入 validation identity；文件指纹不能替代对实际远程运行态的检查。

只有输入、skill、目标和环境身份可比时才考虑非零 baseline cache TTL。默认重新验证基线，避免把上次的硬件占用或旧服务状态当成本轮证据。首次 CPU smoke 通过也不意味着长时间 Day0 服务的所有生命周期和资源恢复都已验证。

`limits.timeouts.run` 默认 86400 秒，`max_wall_seconds` 默认 604800 秒；可以为昂贵任务显式设更长或更短的边界。`max_validation_runs` 约束验证作业数量；`max_model_calls` 计数模型角色作业，**不是 Codex 内部每次模型请求、token 或金额的精确预算**。各角色还受独立 timeout 与日志大小限制。失效证据、资源不可用或环境错误应保留为失败/未知，不用降低精度标准来获得收益。

## 查看、停止、取消与恢复

另一个终端中运行：

```sh
python3 -m rsi_service status --config examples/autonomous/config.json
python3 -m rsi_service events --config examples/autonomous/config.json --tail 20
python3 -m rsi_service stop --config examples/autonomous/config.json
```

`stop` 或停止前台控制器只暂停调度；已启动的独立 worker 会继续。再次 `serve` 使用已有状态并续接同一 worker，避免重复安排昂贵工作。配置或已冻结证据发生变化时，完整性检查会阻止盲目续接。

```sh
python3 -m rsi_service cancel --config examples/autonomous/config.json
python3 -m rsi_service recover --config examples/autonomous/config.json
```

`cancel` 请求取消当前本地 worker 作业；它不是对远端 NPU 服务、容器和脱离进程的通用清理保证。实际 Day0 任务必须自己留下清理/资源释放证据。先检查状态和实际资源，再使用 `recover` 放弃已阻塞的 cycle，在剩余预算内继续；它不会抹去原失败、重置预算或把未知结果变成通过。已经完成的有限实验也不会因再次 `serve` 自动重新开始。

## 可重复的离线集成验证

不调用模型服务的行为集成测试可运行：

```sh
python3 -m unittest discover -s tests -p 'test_autonomous_*.py' -v
```

其中 controller 测试替换模型响应，但实际启动独立 worker、产生失败/成功工具调用、执行检查、保存证据并验证恢复与下一轮回流。它们用于检查服务状态机；上面的真实 Codex CPU smoke 用于检查真实角色调用链。两者都不能替代具体 Day0 任务的硬件验收。
