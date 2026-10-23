# 雷达多租户扇出 —— 函数级落地设计

> 状态：设计已定稿，待实现（B 批次，依赖 B 分区键先落地）
> 上游：`docs/radar_multi_tenant_subscription_design.md`（模型层设计，已归档飞书）
> 关联：`docs/tenant_key_inventory.md`（键清单）、`docs/volatility_campaigns_tenant_partition_design.md`
> 前置：**B 分区键必须先落地** —— 扇出写入的 `VOLATILITY_CAMPAIGNS:{tenant_id}` 与
> 状态键必须先有读取方，否则写了没人读。

## 1. 分层原则（回顾）

| 层 | 内容 | 频率 |
| :--- | :--- | :--- |
| **共享市场层** | `snapshots` / `candles` / SPY / `volatility_threshold_profile` / `volatility_regime` | **一次扫描一次** |
| **租户扇出层** | `positions` / `orders` / campaign 读写 / 状态键 | **每租户一次** |

**判据**：行情与账户无关 → 共享；`positions`/`orders` 与账户有关 → 扇出。

---

## 2. 新增 / 变更函数清单

### 2.1 `tenant.py`（共享入口，与 B 同批）

```python
def active_tenants(registry: dict | None = None) -> list[TenantProfile]:
    """返回 is_active=True 的租户，按 tenant_id 排序（确定性顺序）。"""

def tenant_of_broker(broker) -> tuple[str, str]:
    """返回 (tenant_id, account_id)。

    = broker_scope(broker) + resolve_tenant(account_id=...) + DEFAULT_TENANTS 校验。
    未注册租户 → 返回 ("", account_id)（调用方按 fail-closed 处理）。
    """
```

> `tenant_of_broker` 的**正解已存在于 `risk_limits.py:132 _broker_tenant_id`**，
> 此处是**抽出共享**，不是新写。见 `tenant_key_inventory.md` §G。

### 2.2 `volatility_scanner.py`（拆分）

**新增：共享市场层**

```python
@dataclass(frozen=True)
class MarketScan:
    owners: dict[str, dict]              # symbol -> snapshot 行
    market_move: float                   # SPY 日内涨跌
    bars: dict[str, pd.DataFrame]        # symbol -> K 线
    profiles: dict[str, dict]            # symbol -> volatility_threshold_profile
    regimes: dict[str, dict]             # symbol -> volatility_regime 结果
    skipped: dict[str, str]              # symbol -> 跳过原因（无行情等）

def scan_market_layer(broker, symbols: tuple[str, ...]) -> MarketScan:
    """一次扫描，产出全部与账户无关的中间结果。

    这是**昂贵**的那一层（逐标的 K 线 + 逐标的 regime），必须只跑一次。
    """
```

**新增：租户扇出层**

```python
@dataclass
class TenantScanResult:
    tenant_id: str
    scanned: int = 0
    triggered: int = 0
    campaigns_created: int = 0
    errors: list[str] = field(default_factory=list)

def fan_out_to_tenant(
    store, broker, profile: TenantProfile, market: MarketScan,
    symbols: tuple[str, ...],
) -> TenantScanResult:
    """在**单个租户**的账户上下文中执行扇出。

    只使用 market 里已算好的结果 + 本租户账户的 positions/orders。
    **不得**在此层调用 snapshots/candles。
    """
```

**保留但改签名**

```python
def scan_volatility_watchlist(
    broker, store, symbols: tuple[str, ...] | None = None,
    tenant_id: str | None = None,        # ← 新增
) -> list[dict]:
    """兼容旧调用方：tenant_id 为空时按旧行为（全局键）执行。
    新代码请直接用 scan_market_layer + fan_out_to_tenant。
    """
```

### 2.3 状态键助手（新增，集中拼键）

```python
def volatility_signal_key(tenant_id: str, symbol: str) -> str:
    return f"volatility_signal_active:{tenant_id}:{symbol}" if tenant_id \
        else f"volatility_signal_active:{symbol}"

# 同理：volatility_signal_z / volatility_scanner_notified /
#       volatility_scanner_paused / volatility_candidate_retry_until /
#       volatility_candidate_retry_z / volatility_campaign_rejection
```

> **无前缀形态只作读侧兜底，写侧必须带租户**（与 B 的分区键规则一致）。

### 2.4 `run_radar_service.py`（主循环改造）

```python
# 现状（:107）
broker = FutuPaperBroker(Settings.load(refresh=True))
results = scan_volatility_watchlist(broker, store, symbols)

# 目标
settings = Settings.load(refresh=True)
market = scan_market_layer(market_broker(settings), symbols)   # 共享层，一次
results: list[TenantScanResult] = []
for profile in active_tenants():
    broker = tenant_broker(profile, settings)                  # 长生命周期，缓存复用
    try:
        results.append(fan_out_to_tenant(store, broker, profile, market, symbols))
    except Exception as exc:
        # 失败隔离：单租户失败不影响其他租户，也不影响共享层
        record_tenant_scan_failure(store, profile, exc)
        results.append(TenantScanResult(tenant_id=profile.tenant_id, errors=[str(exc)]))
```

---

## 3. 调用图

```
run_radar_service.main()
  └─ scan_market_layer(market_broker, symbols)          ← 共享，一次
       ├─ load_volatility_symbols()
       ├─ broker.snapshots_tolerant(symbols)
       ├─ broker.snapshots(["US.SPY"])
       ├─ broker.candles(symbol, 120)  ← 逐标的，昂贵
       ├─ volatility_threshold_profile(bars)
       └─ volatility_regime(owner, bars, ...)
  └─ for profile in active_tenants():                  ← 扇出
       └─ fan_out_to_tenant(store, broker, profile, market, symbols)
            ├─ load_volatility_campaigns(profile.tenant_id)      ← 分区键
            ├─ broker.positions() / broker.orders()              ← 账户数据
            ├─ 状态键读写（带租户）
            └─ roll_campaign / upsert_campaign(campaigns_config_key(profile.tenant_id), ...)
```

---

## 4. 失败隔离（红线）

| 层 | 失败影响 | 处置 |
| :--- | :--- | :--- |
| 共享市场层 | **整轮无结果** | 记 ERROR + 退避重试（现状语义） |
| 单租户扇出 | **仅该租户本轮无结果** | 记 `VOLATILITY_TENANT_SCAN_FAILED` + **该租户独立退避**，其他租户照常 |
| 单标的 | 记入 `MarketScan.skipped` | 不中断该租户其他标的 |

**不得**让一个租户的异常终止整个雷达循环（现状的 `try/except` 在 `:141` 包住整轮，
需**下移到单租户粒度**）。

---

## 5. 连接与资源

- `_BrokerConnectionPool` 按 `(host, port, firm, trd_env, account)` 分键 → **每租户一个池**。
- **每租户一个 broker 实例，进程内长生命周期缓存**（`dict[tenant_id, FutuPaperBroker]`），
  **不要每次扫描新建** —— 连接握手昂贵。
- `close_all_connections()`（`:171-173`）在退出时统一关闭，**按池分键，互不影响**。

---

## 6. 自适应间隔

`adaptive_interval_seconds(len(symbols), scan_duration)` 的 `scan_duration` **已包含**
扇出后的总耗时（共享层 + Σ租户层），**公式不用改**。

但需**新增监控**：`volatility_radar_tenant_count`、`volatility_tenant_scan_failures`、
每租户耗时 —— 否则"扫描变慢"无法归因。

---

## 7. 迁移与上线

- **与 B 分区键同批**：扇出写入的分区键必须先有读取方。
- **顺序**：B 分区 → 状态键租户化 → 雷达扇出 → 合并迁移 → 一次重启。
- **中间态禁令**：不得出现"扇出已写带租户键、但状态键清理逻辑只认无前缀"的状态
  （见 `tenant_key_inventory.md` §G.3：③ 是 ①② 的释放条件）。

---

## 8. 验收（交给 Tester）

1. **两租户各拿到自己的 campaign 清单，互不含对方条目**；
2. **一个租户账户不可用时，另一个租户的扫描与写入照常**（故障隔离，端到端）；
3. **共享市场层只调用一次** `candles`/`snapshots`（用调用计数断言，不是断言变量）；
4. **变异测试**：把扇出改回单租户 → 守卫**必须转红**；
5. 雷达**只启动一个进程**（PID 文件 + `start.bat` 判定）；
6. 扫描耗时随租户数可控，三项新指标可见；
7. 状态键租户化后，两租户各自的 `volatility_signal_*` 互不干扰。

---

## 9. 预算键归类（已裁定）

- **`VOLATILITY_RADAR_BUDGETS`（雷达权利金预算）→ 按账户/租户**（Chief 裁定，2026-10-23）。
  依据 Product 自定的切分法：**"凡是钱的参数必须按账户；凡是市场的参数天然全局"** ——
  权利金预算**是钱**，与账户资金量相关，不是市场常量。
- **落地**：`resolve_volatility_radar_budget(symbol)` 需按租户取预算；
  键形与 `risk_limits:{tenant_id}`、`STRATEGY_BUDGETS` 同批处理（Migrator 接线清单）。
- **Product 可推翻此裁定**；若推翻，回到"全局配置"并同步更新 `tenant_key_inventory.md` §D。
