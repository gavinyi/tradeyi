# 欧易币量化与波动率策略扩展设计

## 1. 目标

在现有美股/期权量化系统旁新增独立的币量化域，对接欧易 OKX V5 API，完成：

- 24×7 币种行情与 K 线读取；
- 欧易 Demo Trading 账户、余额、订单、成交和撤单；
- 以实现波动率、价格突破和趋势方向为核心的现货波动率策略；
- 历史回测、Shadow 验证、欧易模拟盘自动执行；
- Streamlit 独立“币量化”工作台；
- 与美股/期权的资金、订单、标的、策略状态和风控完全隔离。

第一版只支持 `SPOT` 现货。`SWAP/FUTURES/OPTION` 仅保留接口扩展点，不实现杠杆、
做空、资金费率、保证金或强平逻辑。

## 2. 为什么不直接复用 Futu Broker

现有 `FutuPaperBroker` 同时绑定了 `US.*` 代码格式、OpenD、交易日历和证券账户语义。
币市场是 24×7，欧易使用 `BTC-USDT` 代码、字符串精度、交易模式 `cash`，并且价格/
数量必须按交易所 `tickSz`、`lotSz` 和 `minSz` 取整。

因此采用“共享能力，隔离交易域”：

- 共享：SQLite 审计、中文 UI、回测指标、告警、运行日志和诊断风格；
- 隔离：欧易凭证、账户、订单、策略状态、标的配置、风险额度和服务心跳；
- 不把欧易返回值伪装成 Futu DataFrame，也不修改 `FutuPaperBroker` 的安全锁。

## 3. 安全边界

### 3.1 强制 Demo Trading

- `OKX_TRADING_MODE` 第一版只接受 `DEMO`；
- 所有私有 REST 请求强制携带 `x-simulated-trading: 1`；
- 基础 URL 只允许官方 HTTPS API 域名，普通国际版默认
  `https://openapi.okx.com`；美国/澳大利亚使用 `https://us.okx.com`，
  欧洲 EEA 使用 `https://eea.okx.com`；
- API Key、Secret、Passphrase 只从环境变量读取，不写入 SQLite、日志或页面；
- 页面只显示“已配置/未配置”，永不回显密钥；
- 自动下单还需 `OKX_ENABLE_DEMO_ORDERS=true`；
- 未通过时钟偏差、产品类型、最小下单量、最大订单金额、数据新鲜度检查时禁止下单。

### 3.2 与现有交易系统隔离

| 领域 | 美股/期权 | 币量化 |
|---|---|---|
| Broker | `FutuPaperBroker` | `OkxDemoBroker` |
| 标的格式 | `US.NVDA` | `BTC-USDT` |
| 策略 ID | `core_satellite` 等 | `crypto_volatility_spot` |
| 心跳 | `trading_service` | `okx_crypto_service` |
| 配置前缀 | `FUTU_` / 现有键 | `OKX_` / `CRYPTO_` |
| 下单开关 | `ENABLE_PAPER_ORDERS` | `OKX_ENABLE_DEMO_ORDERS` |
| 订单归属 | Futu / Shadow | OKX_DEMO / CRYPTO_SHADOW |

任何币策略不得调用 Futu 下单路径，任何美股策略不得读取 OKX 凭证。

## 4. 欧易接口设计

`OkxDemoBroker` 使用可注入的 HTTP transport，便于单元测试，不把网络请求写死在策略中。

公开接口：

- `instruments("SPOT")`
- `ticker(inst_id)`
- `candles(inst_id, bar, limit)`
- `history_candles(inst_id, bar, before, after, limit)`

私有接口：

- `account_balance()`
- `pending_orders(inst_type="SPOT")`
- `order_history(inst_type="SPOT")`
- `fills(inst_type="SPOT")`
- `place_spot_order(request)`
- `cancel_order(inst_id, ord_id)`

签名规则：

`Base64(HMAC_SHA256(secret, timestamp + method + request_path + body))`

错误必须统一转为 `OkxApiError`，错误文本包含：操作、OKX code、原因和可执行建议。

## 5. 统一数据模型

所有金额和数量边界使用 `Decimal`，避免币种小数精度被 `float` 破坏。

核心模型：

- `CryptoInstrument`：代码、基础币、计价币、状态、价格步长、数量步长、最小数量；
- `CryptoTicker`：最新价、买一、卖一、24h 高低/成交额、交易所时间；
- `CryptoOrderRequest`：代码、方向、类型、数量、限价、客户端订单号；
- `CryptoOrderResult`：订单号、客户端订单号、状态、错误码；
- `CryptoVolatilityConfig`：信号、仓位和退出参数；
- `CryptoVolatilityDecision`：动作、强度、目标仓位、原因、逐项检查。

## 6. 波动率策略

### 6.1 输入

默认使用 `1H` K 线，至少 120 根已完成 K 线：

- `rv_fast`：最近 12 根对数收益年化实现波动率；
- `rv_slow`：最近 72 根对数收益年化实现波动率；
- `vol_zscore`：当前绝对收益相对 72 根窗口的 Z 值；
- `breakout`：收盘价相对最近 24 根高低区间的位置；
- `trend`：EMA12 与 EMA48 的方向；
- `volume_ratio`：最近成交量相对 24 根均量；
- `atr_ratio`：真实波幅相对 24 根 ATR。

只使用 `confirm=1` 的完整 K 线生成信号，未完成 K 线仅用于页面观察。

### 6.2 入场

现货只做多。需要同时满足：

1. 波动激活：`rv_fast / rv_slow >= 1.35` 或 `vol_zscore >= 2.0`；
2. 方向向上：`EMA12 > EMA48` 且价格突破最近 24 根区间上分位；
3. 成交确认：`volume_ratio >= 1.10`；
4. 数据新鲜度、点差、交易精度和风险预算通过；
5. 同一标的无待成交买单，且不在冷却期。

信号强度分为观察、标准、强信号三档。目标金额采用波动率反比缩放：

`目标金额 = 标的预算 × 信号系数 × min(1, 目标波动率 / rv_fast)`

### 6.3 退出

退出按优先级判断：

1. 硬止损；
2. 数据异常时禁止新开仓，但不盲目市价平仓；
3. 盈利保护和分批止盈；
4. 趋势反转且波动仍高；
5. 波动收缩连续确认；
6. 最长持有；
7. 否则继续持有。

币策略不复用财报 Gamma 或美股波动率期权的退出状态。

### 6.4 默认参数

| 参数 | 默认值 |
|---|---:|
| K 线 | 1H |
| 快/慢波动窗口 | 12 / 72 |
| 波动扩张倍数 | 1.35 |
| Z 值门槛 | 2.0 |
| 突破窗口 | 24 |
| 最低量比 | 1.10 |
| 单标的预算 | 1,000 USDT |
| 单笔最大金额 | 500 USDT |
| 硬止损 | 8% |
| 第一档止盈 | 12% 平 50% |
| 第二档止盈 | 20% 平 25% |
| 盈利保护启动/回吐 | 10% / 30% |
| 最短/最长持有 | 6h / 120h |
| 退出冷却 | 6h |

这些参数用于快速验证，不代表收益承诺。

## 7. 执行状态机

```text
DISABLED
   |
   v
WATCHING -> SIGNALLED -> ORDER_PENDING -> OPEN
    ^           |              |           |
    |           v              v           v
    +-------- BLOCKED       CANCELLED    EXIT_PENDING
                                           |
                                           v
                                         COOLDOWN
                                           |
                                           +----> WATCHING
```

- 每次决策保存唯一 `decision_id`；
- 客户端订单号由策略、标的、决策时间和动作组成，保证重试幂等；
- `ORDER_PENDING` 未终结前不得重复开仓；
- 下单超时后先查订单，不直接重发；
- 服务重启从 OKX Demo 账户和本地状态重新对账。

## 8. 回测口径

- 信号在完整 K 线收盘计算，下一根开盘成交；
- 买入使用 `ask/slippage` 近似，卖出使用 `bid/slippage` 近似；
- 支持手续费、滑点、最小数量、单笔上限和 24×7 时间轴；
- 禁止同根 K 线看未来数据；
- 输出权益、回撤、交易、胜率、盈亏比、暴露时间、换手率和阻塞原因；
- 后续增加 walk-forward，不用单次全样本参数寻优替代样本外验证。

## 9. 页面信息架构

新增一级菜单“币量化”，首版两个页面：

1. `币量化总览`
   - Demo/Shadow 状态、服务心跳、USDT 权益；
   - 当前持仓及逐笔未实现盈亏；
   - 待成交订单、成交记录；
   - 每个标的最新信号、阻塞原因和目标值；
   - 一键启动/停止服务、撤单。
2. `币策略配置与回测`
   - 页签一：标的与策略参数；
   - 页签二：历史回测；
   - 页签三：API 连接诊断；
   - 凭证只提示 `.env` 变量名，不显示内容。

移动端优先显示状态、权益、持仓盈亏和动作，详细指标放入展开区。

## 10. 失败与救援

| 失败 | 系统行为 | 用户看到 |
|---|---|---|
| 凭证缺失 | 禁止私有请求 | 缺少的变量名与配置说明 |
| Demo Header 缺失 | 客户端构造即失败 | 安全锁拒绝启动 |
| 时钟偏差 | 禁止下单，允许公开行情 | 服务器时间差与修复建议 |
| 限频 | 指数退避，尊重服务端错误 | 下次重试时间 |
| 网络超时 | 查询订单状态后再决定 | “结果未知，正在对账” |
| K 线不足 | 不生成信号 | 当前/要求样本数 |
| 盘口过宽 | 禁止开仓 | 当前点差/上限 |
| 精度不符 | 本地按 tick/lot 向下取整 | 原始值/规范值 |
| 服务重启 | 先对账再恢复扫描 | 对账结果和未解决项 |

## 11. 不在第一版范围

- 欧易真实盘；
- 永续、交割、期权和杠杆；
- 跨交易所套利；
- 高频做市和 L2 订单簿重建；
- 链上钱包、充提币；
- 自动参数寻优后直接上线。

## 12. 验收标准

- 无凭证时公开行情可用，私有功能给出中文可执行错误；
- 任何私有请求都带 Demo Header，任何配置都无法切到真实盘；
- BTC-USDT 可完成行情、回测、Shadow 决策；
- 配置 Demo Key 后可读取模拟余额、订单并在显式开关打开后下模拟单；
- 同一决策重试不产生重复订单；
- 标的精度、最小数量、最大金额和点差门槛都有测试；
- Streamlit 页面静态检查、Ruff 和全量 pytest 通过；
- README、`.env.example`、策略架构和 changelog 更新。

## 13. 官方接口依据

- OKX V5 API 支持 Demo Trading，私有 Demo 请求要求
  `x-simulated-trading: 1`。
- Demo 与生产 REST 可能共用域名，但 WebSocket 地址不同，因此不能只凭域名判断环境。
- Spot 下单使用 `instId`、`tdMode=cash`、`side`、`ordType`、`sz`，限价单需要 `px`。
- 历史 K 线接口返回 `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]`，策略只使用
  `confirm=1`。
