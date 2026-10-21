# 通用多租户架构（N-Tenant Architecture）技术设计规范

> **状态**: 架构评审定稿 (Ready for Implementation)  
> **文档版本**: 2.2 (Multi-Tenant Scalable Architecture - Security Hardened)  
> **制定角色**: Architect (技术架构师)  
> **关联 PRD**: Product《通用多租户架构（N-Tenant）产品需求规格说明书》  
> **适用工程**: `E:\Thisyi\trade` (futu-paper-quant)

---

## 一、 架构泛化定位：从双轨（Dual-Track）到通用多租户（N-Tenant）

用户的远期规划明确指出：系统不仅要支持当前的一个实盘主账号与一个模拟账号，未来还将横向扩展至**多个实盘子账户、多个策略模拟沙盒、甚至跨券商跨市场实例（如 Futu 多账户、IBKR 对冲账户、Alpaca 加密账户等）**。

因此，技术底层坚决杜绝 `REAL / PAPER` 的二元硬编码，全面升级为**基于 `tenant_id` 的通用多租户体系（N-Tenant Architecture）**。

---

## 二、 核心抽象与元数据模型（Tenant Profile）

### 2.1 租户注册表抽象
在 SQLite / 配置中心建立全局租户注册模型 `TenantProfile`：

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str                      # 全局唯一租户主键，例如 "futu_real_master", "futu_paper_sandbox"
    display_name: str                   # 界面可读名称，如 "富途·实盘真金主专户"
    broker_type: str                    # 底座券商，如 "futu", "ibkr", "alpaca"
    trading_env: Literal["REAL", "SIMULATE"]  # 真实资金环境还是沙盒环境
    account_id: str                     # 券商层真实账号（如 "281756481476169109", "20170229"）
    gateway_host: str                   # 网关地址（如 "127.0.0.1"）
    gateway_port: int                   # 网关独立端口（如 11111, 11112）
    risk_profile: str                   # 绑定的风控配置文件或规则 ID
    is_active: bool = True              # 租户启用状态
```

系统内建的默认租户实例映射：
- `futu_real_master`: 富途实盘港股综合主专户（Port 11111，REAL）
- `futu_paper_sandbox`: 富途模拟沙盒专户（Port 11112 或复用 11111，SIMULATE）
- `[未来可水平派生]`: `futu_real_sub01`, `ibkr_options_hedge`, `crypto_sandbox` 等。

---

## 三、 数据层契约全面收敛至 `tenant_id`（Data Partitioning）

所有与交易、持仓、流水、风控相关的底层数据表，均以 `tenant_id` 作为**一级物理分区索引字段**：

### 3.1 意图键与数据库结构规范（安全守卫零破损原则）

#### 1. 意图键（intent_key）字符串格式保持现状：
- **绝对严禁改动 `intent_key` 字符串结构**：意图键必须保持原有的格式（例如 `<strategy_id>:<intent_seq>:<account_id>:<env>` 或 `<strategy>:<symbol>:<side>:<mode>`），**其尾段必须严格保留 `:REAL` 或 `:PAPER`（或 `:SHADOW`）环境后缀**。
- **原因**：现有生产执行核心 `execution.py`（行 352、398、471、498 等）强依赖 `intent_key.rsplit(":", 1)[-1].upper()` 提取环境做守卫校验。若将尾段改为租户 ID，会导致环境解析失效误退化为 SIMULATE，进而造成实盘执行器停止管理实盘订单的重大安全事故。此外，原格式也是金融幂等性的基石。

#### 2. 租户隔离通过独立字段落地（保持原幂等防线）：
```sql
-- 1. 订单意图表：显式增加 tenant_id 显式列，保持原唯一约束 UNIQUE(strategy_id, intent_key)
-- 严格保留全局唯一幂等性，不放宽主键；通过 tenant_id 列与索引进行租户隔离与快速检索
ALTER TABLE order_intents ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_order_intents_tenant ON order_intents (tenant_id);

-- 2. 虚拟头寸分配表 (virtual_allocations)
CREATE TABLE IF NOT EXISTS virtual_allocations (
    tenant_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_cost REAL NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, strategy_id, symbol)
);
```

### 3.2 存储层接口契约（Store API 依赖注入）
废弃所有未指定租户的全局持仓接口，强制传入 `tenant_id`（或依赖注入当前上下文租户）：
```python
def virtual_positions(
    self,
    tenant_id: str,
    strategy_id: str | None = None
) -> list[dict]:
    """严格以 tenant_id 隔离检索虚拟持仓"""
    ...

def create_intent(
    self,
    strategy_id: str,
    intent_key: str,        # 保持原格式不变，尾段必须为 :REAL / :PAPER
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    tenant_id: str,         # 显式入库，用于租户物理分区
    ...
) -> str:
    ...
```

---

## 四、 配置键与运行时状态全量租户化命名空间清单

为了彻底根除双守护进程中“同名策略相互踩踏、全局开关跨租户误伤”的问题，所有运行时配置与动态标志全面升级为**租户命名空间键（Namespaced Config Keys）**：

| 配置/状态键模式 | 说明与防范风险 |
| :--- | :--- |
| **`kill_switch:{tenant_id}`** | 租户级急停熔断开关。模拟盘急停绝不影响实盘，实盘急停只切断实盘。 |
| **`execution_mode:{tenant_id}`** | 租户级全局执行模式（REAL / PAPER / SHADOW），消灭全局单一开关混淆。 |
| **`strategy_execution_mode:{tenant_id}:{strategy_id}`** | 租户下具体策略执行模式覆盖，避免同名策略在实盘与模拟盘模式冲突。 |
| **`strategy_paused:{tenant_id}:{strategy_id}`** | 租户级策略启停与暂停标志，对账异常只暂停故障租户内的策略。 |
| **`circuit_breaker:{tenant_id}:{symbol}`** | 租户级标的熔断与锁仓标记，防止单租户异常扩散。 |
| **`incident:{tenant_id}:{incident_key}`** | 租户级运维与对账告警追踪，报警路由按租户分流。 |

---

## 五、 独立守护进程与水平扩展模型（Worker Process Model）

```text
                               ┌────────────────────────────────────────────────────────┐
                               │           美股全市场实时行情总线 (Quote Bus)             │
                               │           单一 Context 订阅，内存广播，零额度浪费         │
                               └──────────────────────────┬─────────────────────────────┘
                                                          │
                    ┌─────────────────────────────────────┼─────────────────────────────────────┐
                    ▼                                     ▼                                     ▼
     ┌──────────────────────────────┐      ┌──────────────────────────────┐      ┌──────────────────────────────┐
     │  Worker: futu_real_master    │      │  Worker: futu_paper_sandbox  │      │  Worker: [未来新租户 N]       │
     │  --tenant futu_real_master   │      │  --tenant futu_paper_sandbox │      │  --tenant <tenant_id>        │
     │  pid: trading-futu_real.pid  │      │  pid: trading-futu_paper.pid │      │  pid: trading-<tenant_id>.pid│
     │  • 专属性质: REAL 真金白银   │      │  • 专属性质: SIMULATE 前向   │      │  • 专属性质: 独立配置        │
     │  • 挂载交易确认码/限额门禁   │      │  • 专属追单/止盈止损状态机   │      │  • 专属对账与调度闭环        │
     └──────────────┬───────────────┘      └──────────────┬───────────────┘      └──────────────┬───────────────┘
                    │                                     │                                     │
                    └─────────────────────────────────────┼─────────────────────────────────────┘
                                                          │
                                                          ▼
                               ┌────────────────────────────────────────────────────────┐
                               │       🏛️ 统一数据底座 (Tenant Partitioned Store)         │
                               │        按 tenant_id 严格隔离的持仓、流水与审计账本       │
                               └──────────────────────────┬─────────────────────────────┘
                                                          │
                                                          ▼
                               ┌────────────────────────────────────────────────────────┐
                               │       📊 全局多账户视窗驾驶舱 (Streamlit Multi-Tenant)    │
                               │      [当前视窗: 🔴 实盘主账户 ▾] (动态拉取注册表)        │
                               └────────────────────────────────────────────────────────┘
```

1. **水平伸缩（Scale Out）**：
   启动新租户无需重写系统逻辑，仅需配置一条 `TenantProfile`，即可运行：
   `python scripts/run_service.py --tenant <tenant_id>`。
2. **故障域完全隔离（Fault Domain Isolation）**：
   任何一个租户（无论是模拟盘高频赛马，还是新券商接入）发生进程崩溃、网络异常、重试挂起，**其他租户均在独立的操作系统进程中安稳运行，实盘真金核心业务永不受干扰**。
3. **独立的对账与追价闭环**：
   每个 Worker 内部实例化专属于该租户的 `ExecutionManager` 和 `ReconciliationEngine`，负责自身订单的自动追价、超时重挂与三账核对，彻底根除任何环境的孤儿单现象。

---

## 六、 动态风控与安全隔离机制

1. **真实环境安全门禁（Real Trading Gates）**：
   `real_trading.check_real_trade()` 门禁逻辑不硬编码账号，而是**动态绑定至所有 `trading_env == 'REAL'` 的租户**。无论未来接入多少个实盘子账户，只要属于 REAL 环境，均必须严格通过交易确认码、单笔限额、日累计限额、审批指纹的层层校验。
2. **租户级急停熔断（Kill Switch Partitioning）**：
   急停标识严格基于租户隔离：`kill_switch:{tenant_id}`。
   触发某个模拟租户或测试子账户的急停，绝不波及主实盘真金租户。

---

## 七、 实施与演进路线

1. **阶段一：数据契约与 `tenant_id` 分区改造**：
   在 `store.py`、`execution.py` 与 `reconciliation.py` 中引入 `tenant_id` 统一参数，默认提供向后兼容映射（`REAL -> futu_real_master`, `SIMULATE -> futu_paper_sandbox`），保障正在运行的实盘真金服务零停机、零感知。
2. **阶段二：服务参数化与通用 Worker 调度**：
   `run_service.py` 改造为标准租户启动器，支持以服务化脚本分别拉起实盘与模拟 Worker。
3. **阶段三：UI 驾驶舱动态租户切换器**：
   Streamlit 顶部组件改造为从注册表动态拉取租户列表，各业务页面透明支持多租户切片浏览。

---
*本规范已全面按安全审查要求完成修订并重新入库（v2.2 安全加固版）。*
