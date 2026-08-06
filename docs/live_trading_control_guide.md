# 实盘交易控制使用说明

> 当前系统默认仍是富途美股模拟盘。实盘能力只是受控接入框架，必须显式配置、短时确认并通过风控后才会提交真实订单。

## 设计原则

实盘不能做成一个简单开关。系统采用多层闸门：

1. 交易环境必须切到 `REAL`。
2. 实盘总开关 `REAL_TRADING_ENABLED` 必须开启。
3. 必须配置真实账户 `REAL_ACCOUNT_ID`。
4. 策略必须在 `REAL_ALLOWED_STRATEGIES` 白名单内。
5. 必须通过单笔和每日实盘额度。
6. 默认必须开启短时人工确认会话。
7. 仍然要通过原有策略审批、组合风控、订单幂等、急停和对账检查。

系统不会保存富途交易密码。真实账户交易解锁必须在富途 OpenD 或富途客户端中完成。

## 入口位置

在 Streamlit 面板中打开：

`风控与审批 → 实盘交易控制`

页面分为三个 Tab：

- `实盘配置`：配置交易环境、真实账户、策略白名单和额度。
- `短时确认会话`：输入确认码，临时允许实盘订单通过系统闸门。
- `上线检查清单`：检查当前是否满足实盘运行条件。

## 配置项说明

这些配置会写入运行时配置，并同步镜像到 `.env`：

```env
FUTU_TRADING_ENV=SIMULATE
REAL_TRADING_ENABLED=false
REAL_ACCOUNT_ID=
REAL_ALLOWED_STRATEGIES=
REAL_MAX_ORDER_NOTIONAL=500
REAL_MAX_DAILY_NOTIONAL=1000
REAL_REQUIRE_MANUAL_CONFIRM=true
```

字段含义：

- `FUTU_TRADING_ENV`
  - `SIMULATE`：模拟盘，默认值。
  - `REAL`：实盘连接环境。
- `REAL_TRADING_ENABLED`
  - 实盘总开关。即使环境是 `REAL`，这里没开也不会放行。
- `REAL_ACCOUNT_ID`
  - 富途 OpenD 返回的真实账户 `acc_id`。
  - 模拟账户仍使用 `FUTU_ACC_ID`。
- `REAL_ALLOWED_STRATEGIES`
  - 允许实盘运行的策略 ID，逗号分隔。
  - 例如：`core_satellite`
- `REAL_MAX_ORDER_NOTIONAL`
  - 实盘单笔最大名义金额。
  - 期权按 `价格 × 合约数 × 100` 计算。
- `REAL_MAX_DAILY_NOTIONAL`
  - 实盘当日累计最大名义金额。
- `REAL_REQUIRE_MANUAL_CONFIRM`
  - 默认 `true`。
  - 开启后，每次实盘运行前必须在页面开启短时确认会话。

## 推荐上线流程

### 1. 保持模拟盘完成验证

确认当前仍是：

```env
FUTU_TRADING_ENV=SIMULATE
```

先让策略在 `SHADOW` 和 `PAPER` 模拟盘里跑稳定，确认：

- 策略逻辑符合预期。
- 下单数量、限价和方向清楚。
- 订单管理能正常撤单。
- 风控中心没有阻断项。
- 交易诊断中心能看到完整原因。
- 上线审批或豁免规则符合预期。

### 2. 在页面填写实盘配置

进入 `风控与审批 → 实盘交易控制 → 实盘配置`：

1. `券商交易环境` 先保持 `SIMULATE`。
2. 填写 `REAL_ACCOUNT_ID`。
3. 选择允许实盘的策略白名单。
4. 设置很小的单笔/每日额度。
5. 保持 `每次实盘运行都需要短时人工确认会话` 开启。
6. 保存。

建议第一次只开放一个策略，例如：

```env
REAL_ALLOWED_STRATEGIES=core_satellite
REAL_MAX_ORDER_NOTIONAL=100
REAL_MAX_DAILY_NOTIONAL=300
```

### 3. 切换到 REAL

确认富途 OpenD 已登录真实账户后，再在页面把 `券商交易环境` 改为 `REAL`，并保存。

如果配置缺少账户、白名单或额度不合法，页面会拒绝保存，避免系统进入无法加载的坏状态。

### 4. 在富途侧解锁交易

在富途 OpenD 或富途客户端中手动完成真实交易解锁。

本系统不会保存、展示或自动提交富途交易密码。

### 5. 开启短时确认会话

进入 `短时确认会话`：

1. 页面会显示当天确认码，例如 `REAL-20260721-3456`。
2. 手动输入确认码。
3. 设置有效分钟数。
4. 点击 `开启实盘确认会话`。

会话过期后，实盘订单会重新被系统拒绝。

### 6. 将目标策略执行模式改为 REAL

在 `自动化控制中心` 中，把目标策略的执行模式从 `SHADOW` 或 `PAPER` 改为 `REAL`。

只有执行模式实际为 `REAL` 的策略，才会进入实盘安全闸门。

## 下单时的判断链路

实盘订单会按下面顺序处理：

1. 策略生成交易信号。
2. 执行器读取策略执行模式。
3. 如果是 `REAL`，先创建订单意图。
4. 实盘安全闸门检查账户、白名单、额度和确认会话。
5. 原有风险引擎检查持仓、购买力、组合风险、急停和对账状态。
6. 通过后才调用富途 OpenAPI，并使用 `TrdEnv.REAL`。
7. 订单状态进入订单管理、交易诊断、审计事件和绩效链路。

如果被拒绝，可以在：

- `订单管理`
- `交易诊断中心`
- `运行日志`
- `风控中心`
- `实盘交易控制 → 上线检查清单`

查看原因。

## 如何回退到模拟盘

最快回退方式：

1. 打开 `风控与审批 → 实盘交易控制`。
2. 点击 `立即关闭实盘确认会话`。
3. 把 `券商交易环境` 改回 `SIMULATE`。
4. 关闭 `REAL_TRADING_ENABLED`。
5. 保存。
6. 到 `自动化控制中心` 把策略执行模式改回 `SHADOW` 或 `PAPER`。
7. 如有必要，重启统一交易服务。

对应 `.env` 应恢复为：

```env
FUTU_TRADING_ENV=SIMULATE
REAL_TRADING_ENABLED=false
REAL_ACCOUNT_ID=
REAL_ALLOWED_STRATEGIES=
```

## 建议的实盘初始参数

第一次真实验证建议极小额度：

```env
REAL_ALLOWED_STRATEGIES=core_satellite
REAL_MAX_ORDER_NOTIONAL=100
REAL_MAX_DAILY_NOTIONAL=300
REAL_REQUIRE_MANUAL_CONFIRM=true
```

期权策略不建议第一批直接进入实盘。它们对 IV、价差、流动性、追价和到期日更敏感，应该先在模拟盘保留更长样本。

## 安全注意事项

- 不要把 `.env` 提交到 Git。
- 不要把富途交易密码写进 `.env`。
- 不要关闭短时人工确认，除非已经有稳定的外部风控和人工值守。
- 不要一次开放多个策略进入实盘。
- 不要把实盘额度设置得和模拟盘一样大。
- 如果看到侧边栏显示 `REAL 实盘 · US`，请把所有操作都按真实资金处理。
- 急停会阻止新增买入和加仓，卖出平仓仍保持可用。
