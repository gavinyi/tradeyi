# 欧易币量化与波动率策略实施计划

> 严格采用 TDD：每个任务先写失败测试，确认失败原因正确，再做最小实现并回归。

**Goal:** 在不影响 Futu 美股/期权系统的前提下，交付欧易现货 Demo Trading 的行情、
波动率策略、回测、自动执行与独立工作台。

**Architecture:** 新增独立 `futu_quant.crypto_*` 模块和 `OkxDemoBroker`，通过可注入
transport 测试签名与协议；复用 SQLite 审计、回测指标和中文 UI，不复用 Futu 账户/
订单/退出状态。

**Tech Stack:** Python 3.11、pytest、pandas、Streamlit、SQLite、OKX V5 REST API。

## Global Constraints

- 第一版只允许 OKX Demo Trading 和本地 Shadow。
- 第一版只允许 `SPOT`，交易模式固定 `cash`。
- 密钥不得进入数据库、日志、测试快照或页面。
- 测试不得调用真实网络或产生真实订单。
- 所有数量与价格规范化使用 `Decimal`。

### Task 1: 配置、安全锁和领域模型

**Files:**
- Create: `tests/test_okx_config.py`
- Create: `src/futu_quant/crypto_models.py`
- Create: `src/futu_quant/okx_config.py`

- [ ] 写入默认 Demo、禁止 LIVE、凭证完整性、标的格式、金额上限测试。
- [ ] 运行测试并确认因模块缺失而失败。
- [ ] 实现不可变配置与领域模型。
- [ ] 运行专项测试确认通过。

### Task 2: OKX REST 公开行情与私有签名

**Files:**
- Create: `tests/test_okx_broker.py`
- Create: `src/futu_quant/okx_broker.py`

- [ ] 写入签名、Demo Header、响应错误、K 线转换、精度规范化测试。
- [ ] 使用 fake transport 确认测试先失败。
- [ ] 实现 `OkxDemoBroker`，不引入第三方 SDK。
- [ ] 运行专项测试确认通过。

### Task 3: 币波动率信号与退出状态机

**Files:**
- Create: `tests/test_crypto_volatility.py`
- Create: `src/futu_quant/crypto_volatility.py`

- [ ] 写入样本不足、未完成 K 线排除、波动激活、趋势门槛、仓位缩放测试。
- [ ] 写入硬止损、分批止盈、盈利保护、趋势反转、冷却测试。
- [ ] 确认失败后实现纯函数决策器。
- [ ] 运行专项测试确认通过。

### Task 4: 24×7 回测

**Files:**
- Create: `tests/test_crypto_backtest.py`
- Create: `src/futu_quant/crypto_backtest.py`

- [ ] 写入下一根开盘成交、手续费/滑点、无未来函数、最小数量测试。
- [ ] 实现权益、交易和指标输出。
- [ ] 增加基准买入持有对比。
- [ ] 运行专项测试确认通过。

### Task 5: 本地状态、对账和自动执行

**Files:**
- Create: `tests/test_crypto_execution.py`
- Create: `src/futu_quant/crypto_execution.py`
- Create: `scripts/run_crypto_strategy.py`

- [ ] 写入幂等客户端订单号、待成交去重、Shadow/Demo 分流测试。
- [ ] 写入禁用下单、点差过宽、陈旧行情、最大金额测试。
- [ ] 实现单轮执行和 `--loop` 服务。
- [ ] 服务启动先读取模拟账户并对账。

### Task 6: Streamlit 独立币量化页面

**Files:**
- Modify: `app.py`
- Create: `pages/31_币量化总览.py`
- Create: `pages/32_币策略配置与回测.py`
- Modify: `tests/test_streamlit_navigation.py`

- [ ] 先写导航与中文页面存在性测试。
- [ ] 读取 `developing-with-streamlit` skill 后实现页面。
- [ ] 常用状态与配置分离，移动端优先。
- [ ] 运行 Streamlit 静态巡检。

### Task 7: 文档、示例配置与回归

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/strategy_architecture.md`
- Modify: `changelog.md`

- [ ] 写入 Demo API Key 创建和启动说明。
- [ ] 明确现货范围、风险和实盘禁用状态。
- [ ] 运行 Ruff、币模块专项测试、全量 pytest、Streamlit 页面检查。
- [ ] 记录测试结果与剩余限制。

## 执行顺序

`配置/模型 → Broker → 策略 → 回测 → 执行 → 页面 → 文档/回归`

任何阶段测试未通过，不进入下一阶段。
