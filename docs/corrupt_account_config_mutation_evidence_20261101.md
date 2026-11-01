# 坏键 fail-closed 变异证据摘要

- 日期：2026-11-01
- 候选提交：`b4c82e0`
- 分支：`fix/corrupt-account-config-fail-closed`
- 完整原始输出：开发 worktree `data/qa/mutation_evidence_corrupt_config_20261101.txt`；该运行时目录被 gitignore，完整输出另行归档。

## 真实变异实跑

以下均为“修改生产代码 → 执行守卫 → 回滚”的真实变异，不是仅在测试中模拟实现。所有变异回滚后源码 `git diff --stat` 为空。

1. **M1：坏键 enabled 放回 True（fail-open）**
   - 变更位置：`src/futu_quant/real_trading.py:178`
   - 结果：2 failed
   - 失败用例：`test_corrupt_json_is_flagged_and_disabled`、`test_submit_limit_blocks_buy_and_audits_when_corrupt`

2. **M2：移除坏键告警调用**
   - 变更位置：`src/futu_quant/real_trading.py` 的 `notify_corrupt_account_config()`
   - 结果：3 failed
   - 失败用例：`test_corrupt_emits_alert`、`test_corrupt_alert_uses_stable_incident_key`、`test_corrupt_alert_dedupes_into_single_incident`

3. **M3：将 corrupt 强制改为 False（坏键当未配置）**
   - 变更位置：`src/futu_quant/real_trading.py:175`
   - 结果：4 failed
   - 失败用例：包含 `test_buy_blocked_when_account_config_corrupt` 的 BUY 拦截守卫

## 回滚复核

- 回滚后守卫：`19 passed`
- 回滚后 `git diff --stat`：空
- 当前候选源码已恢复至 `b4c82e0`，未将任何变异改动带入候选。

## 结论

三类关键门禁变异均能确定性转红：坏键不得 fail-open、告警不得移除、坏键不得被当作未配置。SELL 仍沿原安全校验路径，不因坏键开旁路。
