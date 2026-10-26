# 故障签名库（e2e 测试样例，格式对齐 AscendBot troubleshooting-signatures.md）

## §1 签名总表

### 启动期

| 签名 ID | grep 正则 / 错误码 | 根因 | 即时处置 | 流程路由 | 适用版本 |
|---|---|---|---|---|---|
| SIG-901 | `RuntimeError: HCCL error.*binding failed` / E90101 | 残留服务占端口 | 精确清理 | 环境类 | 通用 |
| SIG-902 | `ValueError: shape mismatch` | 权重映射缺漏 | ——（不可即时处置） | Developer 返工 | 通用 |
| SIG-903 | （无正则占位行） | 测 needs_review 路径 | —— | 不路由 | 通用 |
