# 永续合约 Managed Futures + 山寨币空头恢复策略

日期：2026-07-26

## 背景

上一版永续合约策略偏“短周期波动突破”，多空都做。最近回测显示它在币本位/合约市场里噪音太多，容易频繁追涨杀跌。该逻辑更像适合美股期权事件/波动率表达，不适合作为当前合约自动交易主线。

本轮将合约主线改为：

- `MANAGED_FUTURES` 主引擎；
- 山寨币弱势做空；
- 小批量首仓；
- 亏损后允许受限补仓；
- 但必须重新通过弱势评分，不允许无限马丁格尔；
- BTC/ETH 默认排除做空。

## 策略思想

用户输入的原始想法是：“只做空其他山寨币，小批量，亏损就继续补仓，一直等到它转正，BTC 这种正统币不适合做空。”

行业化后不能直接做无限补仓，因为合约山寨币会出现短时暴拉、流动性断层和强平风险。因此本系统采用“受限恢复”：

1. 只对非 BTC/ETH 的弱势山寨币开空；
2. 首仓只使用单标的预算的一小部分；
3. 亏损达到阶梯后，只有弱势评分仍通过才补仓；
4. 补仓层数、金额、单笔最大亏损都有硬上限；
5. 转正达到恢复止盈后退出；
6. 如果趋势质量失效、资金费率过度不利、强平缓冲过低，则退出。

## 当前默认参数

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `CRYPTO_SWAP_ENGINE` | `MANAGED_FUTURES` | 合约主引擎 |
| `CRYPTO_SWAP_ALT_SHORT_ENABLED` | `true` | 启用山寨币空头恢复 |
| `CRYPTO_SWAP_ALLOW_MAJOR_SHORT` | `false` | BTC/ETH 默认不允许做空 |
| `CRYPTO_SWAP_ALT_SHORT_EXCLUDED_BASES` | `BTC,ETH` | 空头排除币种 |
| `CRYPTO_SWAP_MF_SCORE_THRESHOLD` | `65` | 弱势评分门槛 |
| `CRYPTO_SWAP_MF_RELATIVE_WEAKNESS` | `-0.015` | 近 72H 相对 BTC 至少弱 1.5% |
| `CRYPTO_SWAP_MF_MIN_VOLUME_RATIO` | `0.80` | 最低成交量比 |
| `CRYPTO_SWAP_MF_MAX_NEGATIVE_FUNDING_SHORT` | `-0.001` | 空头最大不利资金费率 |
| `CRYPTO_SWAP_ALT_SHORT_INITIAL_MARGIN_FRACTION` | `0.35` | 首仓使用单标的保证金预算的 35% |
| `CRYPTO_SWAP_ALT_SHORT_ADD_LOSS_STEPS` | `0.03,0.06,0.10` | 亏损补仓阶梯 |
| `CRYPTO_SWAP_ALT_SHORT_ADD_MARGIN_MULTIPLIERS` | `1.0,1.2,1.5` | 各层补仓金额倍数 |
| `CRYPTO_SWAP_ALT_SHORT_MAX_LAYERS` | `3` | 最大补仓层数 |
| `CRYPTO_SWAP_ALT_SHORT_RECOVERY_TAKE_PROFIT` | `0.008` | 转正止盈 |
| `CRYPTO_SWAP_ALT_SHORT_MAX_POSITION_LOSS` | `0.12` | 单笔恢复硬止损 |
| `CRYPTO_SWAP_MF_TREND_INVALIDATION_SCORE` | `45` | 空头趋势失效评分 |
| `CRYPTO_SWAP_MF_BENCHMARK_SYMBOL` | `BTC-USDT-SWAP` | 相对弱势基准 |

## 评分维度

山寨币空头评分包含：

- 1H 动量：近 24H、72H、120H 是否持续走弱；
- 相对弱势：近 72H 是否跑输 BTC；
- 量能：是否有足够成交量；
- 4H/高周期 EMA 空头排列；
- 日线 EMA 空头排列；
- 资金费率是否不过度不利；
- 年化实现波动过高时扣分。

## 执行语义

`OPEN_SHORT` 在无仓时表示首仓；在已有同向空仓时表示补仓。Shadow 账本会合并张数并重算平均成本。

平仓触发包括：

- 转正恢复止盈；
- 单笔硬止损；
- 强平缓冲过低；
- 空头资金费率恶化；
- 趋势质量失效；
- 旧的分批止盈/盈利保护兜底。

## 安全默认

本轮只切换主引擎和策略结构，不自动打开新开仓总开关。是否允许新增合约仓位仍由：

```env
CRYPTO_SWAP_ENTRY_ENABLED=true|false
```

控制。
