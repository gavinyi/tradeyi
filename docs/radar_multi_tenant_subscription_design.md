# 雷达多租户订阅模型设计

> 状态：设计已定稿，待实现（与 B 项同批上线）
> 关联：`docs/volatility_campaigns_tenant_partition_design.md`、`docs/dual_tenant_architecture_tech_spec.md`
> 背景：2026-10-22 夜间，雷达产出的机会只进了实盘租户，模拟租户没有自己的机会来源；
> 根因是整条机会链（campaign 清单 + 信号状态键）都是**单租户形状**的全局键。

## 1. 现状（单租户形状）

| 位置 | 行为 |
| :--- | :--- |
| `scripts/run_radar_service.py:107` | `FutuPaperBroker(Settings.load(refresh=True))` —— **一个进程、一个账户**（默认账户） |
| `volatility_scanner.py:87` `scan_volatility_watchlist(broker, store, symbols)` | 与 broker 绑定，单账户 |
| `volatility_scanner.py:366` | 写全局 `VOLATILITY_CAMPAIGNS` |
| `volatility_scanner.py:167-170` | 写全局 `volatility_signal_active:{symbol}` / `volatility_signal_z:{symbol}` |
| `volatility_scanner.py:189` | 写全局 `volatility_scanner_notified:{symbol}` |
| `volatility_scanner.py:129` | 读全局 `volatility_scanner_paused:{symbol}` |
| `volatility_scanner.py:364-365` | 写全局 `volatility_candidate_retry_*:{symbol}` |

**后果**：REAL 与 PAPER 两侧每轮打印的 campaign 完全相同；雷达的机会只喂默认账户。

## 2. 目标形态

**一次扫描（共享）→ 按租户扇出（隔离）→ 各租户只对自己的账户下单。**

```
        ┌────────────────────────── 共享层（与账户无关）──────────────────────────┐
        │ load_volatility_symbols() / snapshots / candles / SPY market move        │
        │ volatility_threshold_profile() / volatility_regime()                       │
        └──────────────────────────────────────────────────────────────────────────┘
                                      │ 一次
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              租户 A 扇出        租户 B 扇出        租户 N 扇出
        （各自账户: positions/orders/campaign 读写/信号状态）
                    │                 │                 │
                    ▼                 ▼                 ▼
        VOLATILITY_CAMPAIGNS:A   :B                :N
```

**关键判断**：行情数据与账户无关，**只取一次**；账户相关的只有 `positions` / `orders` 与 campaign/状态键。

## 3. 租户枚举与解析

- 租户来源：`tenant.DEFAULT_TENANTS`，取 `is_active=True` 的条目。
- 租户 → 账户：`tenant.resolve_tenant(tenant_id=...)`，**不新增映射表**。
- 账户 → broker：`FutuPaperBroker(settings, trading_env_override=..., account_id_override=...)`，
  与 `run_service.py:972` 既有用法一致。
- **租户解析失败必须抛异常**，不得静默回落默认账户（沿用既有 fail-closed 口径）。

## 4. 状态键租户化（与 B 项同批）

| 现状（全局） | 目标（租户键优先 + 无前缀兜底） |
| :--- | :--- |
| `volatility_signal_active:{symbol}` | `volatility_signal_active:{tenant_id}:{symbol}` |
| `volatility_signal_z:{symbol}` | `volatility_signal_z:{tenant_id}:{symbol}` |
| `volatility_scanner_notified:{symbol}` | `volatility_scanner_notified:{tenant_id}:{symbol}` |
| `volatility_scanner_paused:{symbol}` | `volatility_scanner_paused:{tenant_id}:{symbol}` |
| `volatility_candidate_retry_until:{symbol}` | `volatility_candidate_retry_until:{tenant_id}:{symbol}` |
| `volatility_candidate_retry_z:{symbol}` | `volatility_candidate_retry_z:{tenant_id}:{symbol}` |

读侧规则统一：**租户键优先、无前缀旧键回退**。写侧必须显式传 `tenant_id`，缺失即抛异常。

## 5. 连接与资源

- `_BrokerConnectionPool` 已按 `(host, port, firm, trd_env, account)` 分键（`broker.py:211-227`）
  → **每租户一个池，天然隔离**，扇出不需要新建连接层。
- **每租户一个 broker 实例，长生命周期复用**（不要每次扫描新建）—— 连接握手昂贵。
- 扫描耗时 = 共享层 + Σ(租户层)。租户层只有 `positions` / `orders` 两次调用，很轻。
- `adaptive_interval_seconds(len(symbols), scan_duration)` 已按 `scan_duration` 自适应，
  **公式不用改**，但需把扇出后的耗时纳入监控（见 §8）。

## 6. 失败隔离（关键红线）

- **单租户失败不得中断其他租户**：每个租户的扇出包在独立 `try/except` 中。
- 失败记 `VOLATILITY_TENANT_SCAN_FAILED`（ERROR）+ 该租户独立退避，**共享层与其他租户照常**。
- **不得让一个租户的异常导致整个雷达退出** —— 雷达是低频发现流程，缺一个租户的这轮扫描，
  远比整条链停摆好。

## 7. 并发模型

- **顺序扇出**（不并行）：OpenD 是本地网关，并行加压无益；租户层轻，顺序足够快。
- 未来租户数大增时，再演进为 asyncio / actor 池 —— 与团队既定的
  **Tiered Multi-Tenancy 演进路径**（Silo → 真金独立 Worker + 共享异步池）一致。

## 8. 可观测性

- 每轮记录：`scan_duration`、租户数、各租户耗时、各租户触发数。
- 新增指标 `volatility_radar_tenant_count` 与 `volatility_tenant_scan_failures`。
- 心跳文案区分"共享层完成"与"各租户扇出结果"，便于定位是哪个租户卡住。

## 9. 迁移与上线顺序

- 雷达改造与 B 项**同批上线**（分区键必须先有读取方，否则写了没人读）。
- 正确顺序：**B 分区代码 → 雷达扇出 → 合并迁移 → 一次重启**，中间不起服务、不改代码。

## 10. 红线

1. **单租户失败不拖垮其他租户**。
2. **租户解析失败抛异常**，不静默回落默认账户。
3. **不新增第二个雷达进程**（两条雷达会互相覆盖同一份清单）。
4. **共享层只取一次行情**，不得按租户重复订阅。
5. **状态键与 campaign 键同步租户化**，不留"半个全局、半个租户"的混合态。

## 11. 验收（交给 Tester）

1. 两个租户各拿到自己的 campaign 清单，**互不含对方条目**；
2. **一个租户账户不可用时，另一个租户的扫描与写入照常**（故障隔离）；
3. 全量回归 + 变异测试：把扇出改回单租户，守卫**必须转红**；
4. 雷达**只启动一个进程**（PID 文件 + `start.bat` 判定）；
5. 扫描耗时随租户数可控，指标可见；
6. 状态键租户化后，两租户各自的 `volatility_signal_*` 互不干扰。
