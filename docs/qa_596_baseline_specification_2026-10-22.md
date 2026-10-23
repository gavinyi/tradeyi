# Tradeyi 项目：测试基线与守卫清单（2026-10-22 ~ 10-23）

> 归档位置：03_测试基线与用例
> 维护：Tester　|　上一次全量回归：**2026-10-23 00:39（本地）**
> 基线结论：**596 passed / 0 failed，exit code 0，3 分 29 秒**

---

## 一、 全量回归基线

| 项 | 值 |
| :--- | :--- |
| 命令 | `E:\Thisyi\trade\.venv\Scripts\python.exe -m pytest --basetemp=data/qa/run_full_p0` |
| 用例总数 | **596** |
| 结果 | **596 passed in 209.60s (0:03:29)** |
| 退出码 | `0` |
| 前置 | Windows 沙箱 workspace-write；严禁 `2>&1` / `| Select-Object` 管道（会触发沙箱 EPERM） |

> 历史基线：2026-10-20 为 507/507（见 `qa_507_baseline_specification.md`）。本日新增 89 条，主要来自多租户隔离与持仓采用时效两条主线。

---

## 二、 本日新增守卫清单（13 个文件 / 110 条）

### 2.1 实盘账本原生底仓偏移与租户隔离（`test_real_ledger_native_offset.py`，9 条）

| 用例 | 守护的不变量 |
| :--- | :--- |
| `test_real_ledger_counts_own_account_native_offset` | 实盘账本必须计入**本账户**的 `reconciliation_adjustments` 偏移（-1 + 8 = 7） |
| `test_real_ledger_offset_isolated_by_account` | 偏移按账户物理隔离；实盘偏移不得污染模拟账本 |
| `test_reconcile_real_native_holdings_zeroes_three_ledgers` | 计入偏移后三账归零（MATCHED）、策略不被熔断、`recovery_blocked=false` |
| `test_legacy_empty_tenant_adjustments_do_not_leak_into_real_ledger` | `''/''` legacy 分区的 7 月旧值（NVDA +211）不得进入实盘账户视图 |
| `test_empty_tenant_virtual_allocation_does_not_leak_into_real_ledger` | 空租户 `virtual_allocations` 行不得串入实盘账本（旧行为实测 7.0 → 57.0） |
| `test_legacy_orphan_adjustments_never_enter_any_ledger_view` | legacy 孤儿行不得回流**无账户视图**（旧行为会得到"数值恰好等于实盘持仓"的假象） |
| `test_manual_reconcile_sync_writes_account_scoped_offset` | 一键平账写入的偏移必须带账户维度，且只影响该账户 |
| `test_launch_approval_page_passes_tenant_and_account_to_sync` | **AST 静态守卫**：UI 一键平账必须传 tenant/account（漏传会写进 legacy 分区） |
| `test_adjustment_table_requires_tenant_and_account_columns` | 表结构契约：缺租户/账户维度列即明确失败 |

### 2.2 对账锁生命周期与恢复语义（`test_risk_reconciliation_isolation.py`，16 条）

| 用例 | 守护的不变量 |
| :--- | :--- |
| `test_fine_grained_reconciliation_risk_isolation` | 标的级 / 策略级精准阻断；独立策略不被连坐；平仓通道不阻断 |
| `test_reconciliation_isolation_fallback_without_strategy_id` | 无策略 ID 时才走全局兜底；SHADOW 豁免 |
| `test_reconciliation_isolation_tenant_id_prefix` | **生产口径 broker（无 `tenant_id`）下，租户前缀键必须仍能阻断** |
| `test_reconciliation_isolation_account_prefix_blocks_and_isolates` | 账户前缀键阻断本账户、不误伤其他账户同名策略 |
| `test_stale_account_prefixed_lock_is_cleared_on_recovery` | 陈旧账户前缀锁（含 `strategy_enabled` / `restore` 同族键）必须随恢复清除 |
| `test_recovery_does_not_clear_other_tenants_real_lock` | 本租户恢复扫描**绝不清**别家租户的真实对账锁 |
| `test_recovery_is_idempotent` | 恢复扫描幂等：连跑两次，第二次不得再改动任何键 |
| `test_recovery_restores_user_disabled_tenant_strategy` | 暂停前用户主动停用的租户键，恢复后必须仍是停用 |
| `test_recovery_restores_both_prefixed_key_forms` | 带前缀的**两种拼法**（`{prefix}core_satellite` / `{prefix}Core Satellite`）都必须进快照并还原 |
| `test_recovery_falls_back_to_enabled_only_without_snapshot_record` | **已反转**：快照记录为空 = pause 前该键不存在 = 恢复后必须保持未设置（原断言把缺陷写成了预期） |
| `test_recovery_keeps_absent_tenant_key_absent` | 读侧可见结果：pause 前后 `resolve_configured_strategies` 返回同一集合 |
| `test_disable_pauses_all_enable_key_forms` | 对账暂停必须把**全部**启用键形式置 `false`（写侧是读侧的超集） |
| `test_recover_does_not_create_snake_key_when_only_display_name_existed` | 只设了显示名键时，恢复不得凭空创建蛇形键 |
| `test_quick_launch_writes_tenant_scoped_enable_key` | **AST 静态守卫**：UI「一键上线」必须写租户级键、不得写无前缀全局键 |
| `test_alert_manager_whitelisted_ledger_mismatch_not_swallowed_when_incident_already_open` | 白名单致命事件不得被静默吞噬 |
| `test_alert_manager_option_lifecycle_risk_is_whitelisted` | 期权生命周期风险事件在白名单内 |

### 2.3 持仓采用时效（P0-1/P0-2 验收，`test_position_adoption_latency_guards.py`，6 条）

| 用例 | 守护的不变量 |
| :--- | :--- |
| `test_real_env_deal_push_records_fill` | **REAL 环境成交必须进入 `record_fill`**（旧行为被 SIMULATE 过滤，真金侧存在无保护窗口） |
| `test_simulate_env_deal_push_still_records_fill` | 回归：SIMULATE 成交的既有行为不得被破坏 |
| `test_fill_with_active_campaign_registers_immediately` | 成交时该 code 有活跃 campaign → **同一次推送内**标记登记，不等周期 |
| `test_fill_without_owner_adopts_immediately` | 成交时无归属 → **当场**建 `exit_only` campaign |
| `test_radar_roll_path_uses_shared_retain_invariant` | **AST 调用点**守卫：雷达滚动必须**调用** `retain_campaign_if_at_risk`（只 import 不调用 = 护栏不存在） |
| `test_protection_coverage_metric_exists` | 保护覆盖率必须成为常驻指标（< 1.0 立即 ERROR 告警） |

### 2.4 其余本日新增 / 加固

| 文件 | 条数 | 守护内容 |
| :--- | :--- | :--- |
| `test_roll_does_not_orphan_filled_position.py` | 5 | 滚动用**新鲜持仓快照**；已成交 code 不得被顶掉；`has_recorded_fill` 读成交流水 |
| `test_real_daily_notional_excludes_sells.py` | 4 | **卖出不占用当日开仓预算**；买入仍计入；被排除的方向受尊重 |
| `test_order_submit_and_cancel_probe.py` | 4 | $0.01 挂单 + 毫秒撤单全链路；跨环境撤单守卫；撤单幂等 |
| `test_launch_workers.py` | 5 | 活跃租户筛选；PID 存活契约；锁/日志/PID 文件物理隔离；**幂等防重拉**；单租户故障隔离 |
| `test_tenant_strategy_enable_keys.py` | 5 | 租户启用键两种拼法都生效；租户键优先于全局；跨租户不泄漏 |
| `test_reconciliation_tenant_adjustments.py` | 4 | 偏移表必须有租户/账户维度；实盘账本计入本账户偏移；legacy 不污染已知账户 |
| `test_tenant_context_guards.py` | 11 | 租户上下文解析严格化；跨租户意图隔离；**实盘对账差异不得暂停模拟策略**；急停双写 |
| `test_service_stop_semantics.py` | 5 | 租户级 stop 文件能唤醒等待；全局 stop 仍能唤醒；独立脚本不复用全局 stop 名 |
| `test_option_exit_quality_guard.py` | 8 | 期权出场盘口健康校验；NaN/None 鲁棒性；临界阈值精确拦截；零成交容错 |
| `test_execution.py` | 26 | 含**精细化隔离的语义改写**（全局不再一刀切连坐） |

---

## 三、 守卫设计纪律（本日血的教训）

1. **断言必须落在读侧可见结果上**，不能只查名字存在。
   反例：`test_radar_roll_path_uses_shared_retain_invariant` 原先只查字符串 `"retain_campaign_if_at_risk" in scanner` —— `import` 了但没调用照样通过，第一次变异就没抓住。已改为 AST 调用点检测。
2. **断言了一个代码根本不会写的键 = 假绿灯**。
   实例：`test_recovery_falls_back_to_enabled_only_without_snapshot_record` 曾把缺陷行为（"无快照记录时回落 true"）写成预期，580 全绿实际在守护 bug。
3. **守卫必须能真正反映被测代码的当前状态**（模块加载绑定陷阱）。
   实例：变异测试脚本重载 `futu_quant.store` 后，守卫模块里 `from futu_quant.store import TradingStore` 仍指向**旧类对象**，变异根本没被测到（M1/M2 一度"未捕捉"）。修法：重载子模块后必须**重新绑定守卫模块里的类引用**。凡是"重载/替换被测对象"的守卫，都要确认引用是活的。
4. **变异测试是强制的，且在副本上做**。
   把被测行为改回旧写法 → 守卫必须转红；改完精确还原并用 `git diff --stat` 校验工作树未变。
5. **写侧必须是读侧的超集**：新增/修改任何按租户隔离的键时，先确认写侧覆盖了读侧认的**每一种拼法 × 每一种前缀变体**。本日七层缺陷里有四层是这一条的违反。

---

## 四、 变异测试记录（全部通过）

| 变异（改回旧写法） | 转红的守卫 |
| :--- | :--- |
| restore 快照只记无前缀键 | `test_recovery_restores_user_disabled_tenant_strategy`、`test_recovery_restores_both_prefixed_key_forms` |
| 恢复扫描只认租户前缀 | `test_stale_account_prefixed_lock_is_cleared_on_recovery`、`test_recovery_does_not_clear_other_tenants_real_lock`、`test_recovery_is_idempotent` |
| 按「末段 == 策略名」朴素匹配 | `test_recovery_does_not_clear_other_tenants_real_lock` |
| `restore.get(key, "")` 改回 `or "true"` | `test_recovery_falls_back_to_enabled_only_without_snapshot_record`、`test_recovery_keeps_absent_tenant_key_absent` |
| 恢复 SIMULATE 过滤 | `test_real_env_deal_push_records_fill` |
| 雷达路径不再调用共享护栏 | `test_radar_roll_path_uses_shared_retain_invariant` |
| 去掉保护覆盖率指标 | `test_protection_coverage_metric_exists` |
| 去掉成交即登记 | `test_fill_with_active_campaign_registers_immediately`、`test_fill_without_owner_adopts_immediately` |
| 一键上线只写全局键 | `test_quick_launch_writes_tenant_scoped_enable_key` |
| A-1 去掉跨来源累计查询 | `test_deal_push_then_order_push_records_once`、`test_filled_quantity_reset_then_re_reported_does_not_double_count`、`test_genuine_increments_are_not_suppressed`、`test_over_count_is_detected_when_it_occurs`、`test_partial_fills_with_alternating_paths` |
| A-2 去掉 deal push 跨路径认领 | `test_order_push_then_deal_push_records_once`（断言 source 升级 + `broker_deal_id` 保持 NULL） |
| A-3 去掉过计预防（无条件插入） | `test_deal_push_then_order_push_records_once`、`test_filled_quantity_reset_then_re_reported_does_not_double_count`、`test_over_count_is_detected_when_it_occurs`、`test_partial_fills_with_alternating_paths` |
| A-4 去掉过计告警出口 | `test_over_count_is_detected_when_it_occurs` |

变异脚本：`data/qa/mutation_test_copy.py`、`data/qa/mutation_test_page.py`、`data/qa/mutation_test_adoption.py`、`data/qa/mutation_test_fill_idempotency.py`（均为副本式，不碰工作树）。

### A 的过计守卫（预防 + 探测成对，已转绿）

| 用例 | 守护的不变量 | 状态 |
| :--- | :--- | :--- |
| `test_over_count_path_is_prevented` | Reviewer 推演的序列（order push 先报累计 1.0 → 两笔 0.5 deal push 分笔到达）**不得产生过计**（账本累计 ≤ 券商累计） | 🟢 |
| `test_over_count_is_detected_when_it_occurs` | 过计万一漏出，下一轮上报必须检出并留下 `FILL_LEDGER_OVER_COUNT` 告警/事件（带 `intent_id`） | 🟢 |

**实现位置（A 全批，附行号备核）**：`store.py:1242-1268`（`update_by_broker_order` 的三分支：过计 / 幂等命中 / 真实增量）、`store.py:2645-2668`（`record_fill` 的过计护栏，只对 `BROKER*` 来源生效）、`store.py:1092-1122`（`_emit_fill_overcount`：audit + alert + operational_event 三处留痕）、`store.py:2612-2634`（跨路径认领，只升级 `source`、不写 `broker_deal_id`）。窗口统一为常量 `FILL_PROXY_MATCH_WINDOW_SECONDS` / `FILL_DUP_MATCH_WINDOW_SECONDS`。

**防御纵深说明**：`record_fill` 的过计护栏（`:2645-2668`）在认领被摘掉时仍会兜底跳过，因此**认领的可见效果是 source 升级**而非双计防护——守卫已按此口径断言（只认领效果 + `broker_deal_id` 保持 NULL）。

---

## 五、 已知未关闭项（不阻塞本日基线）

| 项 | 内容 | 归属 |
| :--- | :--- | :--- |
| P2 | `_recover` 分辨不出「暂停期间有人工**停用**」（暂停自己写的也是 `false`）；已写进注释，待办用键 `updated_at` 与暂停时刻比对 | Coder |
| P2 | 四个无账户调用点（`risk.py`、`option_lifecycle.py`、`connectivity_protection.py`、`pages/8_runtime_log.py`）仍读跨租户混合账本（假数据已清，语义建议收口） | Coder |
| 待落 | P0-3 止损 fail-closed 的 4 条验收（所有 campaign 有显式值 / 缺键 → ERROR + 拒绝开仓 / **退出管理不受影响** / 两层取较紧者写进注释与 UI） | Coder 落地后 Tester 落守卫 |
| 待落 | ④b 跨租户暂停键：在实盘租户执行「暂停全部策略」后，模拟租户策略循环仍运行 | Coder 落地后 Tester 落守卫 |
| 待落 | `pages/15_strategy_control_center.py` 文案与 `strategies_paused` 实际语义相反（界面称"不会停止退出管理"，代码 `configured.clear()` 后退出管理全停） | 需产品口径 |

---

## 六、 测试执行环境铁律（Windows 沙箱）

1. **严禁管道捕获**：`2>&1`、`| Select-Object`、`Tee-Object` 均会触发沙箱命名管道 EPERM，导致"程序无法运行 / Access is denied"。
2. **直连 venv Python**：`E:\Thisyi\trade\.venv\Scripts\python.exe -m pytest`，显式 `--basetemp=data/qa/run_xxx`。
3. **全量走后台**：`run_in_background: true`，靠系统回调收口；前台超时会被自动转后台。
4. **子进程禁管道**：变异脚本内不得 `subprocess.run(capture_output=True)`（同样 EPERM），改为进程内重载模块。
5. **`futu` 导入前重定向 APPDATA**：`futu` 的 FTLog 会写 `%APPDATA%\com.fututn.FutuOpenD\Log\`，沙箱下不可写；变异脚本需先设 `APPDATA` 到工作区内部。
