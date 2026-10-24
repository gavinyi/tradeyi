# 单策略粒度「暂停新开仓 (Reduce-Only)」技术架构设计方案

> **设计文档**: docs/per_strategy_pause_entries_design.md  
> **文档版本**: 1.0 (Architecture Design)  
> **责任人**: Architect (架构师)  
> **对齐需求**: docs/per_strategy_pause_entries_prd.md (Product)  
> **状态**: 待 Chief 审批签字与计划排期

---

## 一、 架构设计原则与边界

1. **Reduce-Only 铁律（绝对单向放行）**：
   - 拦截仅且必须仅作用于 **`side == BUY`**（开仓/增仓）；
   - **`side == SELL`**（止盈、止损、减仓、临期处置、强平）必须无条件、100% 顺畅放行，任何人不得在退出路径上加设阻断逻辑。
2. **多租户严格隔离（N-Tenant 命名空间）**：
   - 存储键与判定逻辑强绑定 `tenant_id`，杜绝实盘与模拟盘配置互相污染。
3. **双层拦截架构（防透穿 + 防空转）**：
   - **L1 策略层早拦（Skip Entry Scanning）**：策略主循环在扫描新机会时提前阻断，避免无效计算与高频刷屏日志；
   - **L2 执行层底线硬门禁（Execution Gate）**：`ExecutionManager.submit_limit` 作为全局统一底线守卫，拦截所有漏网的 BUY 意图，形成不可绕过的安全闭环。
4. **单源解析原则**：
   - 页面与所有策略执行路径必须统一调用**生产解析函数**，严禁私自硬编码拼接配置键名。

---

## 二、 配置契约与解析机制

### 1. 存储键规范
统一保存在 `system_settings` / `runtime_config` 表中：
- **标准键格式**：`strategy_entries_paused:{tenant_id}:{strategy_id}`（如 `strategy_entries_paused:futu_real_master:volatility_long_gamma`）
- **值规范**：`"true"` / `"false"`（全小写字符串）。

### 2. 生产统一解析函数设计
在 `src/futu_quant/strategy_pause.py`（新增轻量级工具模块）提供标准解析函数：

```python
def is_strategy_entries_paused(store, strategy_id: str, tenant_id: str = "") -> bool:
    """
    解析指定租户下的指定策略是否开启「暂停新开仓 (Reduce-Only)」。
    优先级与兼容回退链：
    1. 租户标准键: strategy_entries_paused:{tenant_id}:{strategy_id}
    2. 波动率策略历史租户键兼容: volatility_new_entries_paused:{tenant_id} (仅限 volatility_long_gamma)
    3. 全局策略键兼容: strategy_entries_paused:{strategy_id}
    4. 波动率策略历史全局键兼容: volatility_new_entries_paused (仅限 volatility_long_gamma)
    5. 默认值: False (放行)
    """
```

- **解析逻辑说明**：
  - 遇到空值或不存在键时，明确返回 `False`（未开启暂停，正常开仓）；
  - 若解析到非法格式字符串，记录 WARNING 审计日志并兜底为 `False`。

---

## 三、 核心改动点与调用路径

### 1. 执行层底线硬门禁（L2 统一守卫）
**修改文件**：`src/futu_quant/execution.py`  
**挂载位置**：`ExecutionManager.submit_limit`（在批次安全审批与实盘门禁前后、执行风控检查前）：

```python
# --- Reduce-Only / 暂停新开仓门禁 ---
if side.upper() == "BUY":
    tenant_id = getattr(self.tenant, "tenant_id", "") if self.tenant else ""
    if is_strategy_entries_paused(self.store, strategy_id, tenant_id):
        reason = (
            f"策略「{strategy_id}」在租户「{tenant_id or 'default'}」已开启「暂停新开仓」："
            f"坚决不建新仓，已有持仓继续由策略执行止盈、止损与退出管理"
        )
        self.store.audit("STRATEGY_NEW_ENTRY_BLOCKED", {
            "strategy_id": strategy_id,
            "tenant_id": tenant_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
        })
        return ExecutionResult(0, "REJECTED", reason)
```

**关键设计**：`side.upper() == "SELL"` 绝对跳过此块逻辑，保证退出通道畅通。

### 2. 策略主循环早拦（L1 决策层）
**修改文件**：
- `scripts/run_volatility_strategy.py` / `src/futu_quant/volatility_roll.py`：
  - 在扫描与生成新开仓候选之前调用 `is_strategy_entries_paused`，若为 `True`，跳过 `rank_long_option_candidates` 与新订单提交；
  - **保留并继续执行**：`diagnose_campaign`、组合平仓（`close_stale_expired_combos`）、动态追踪止盈止损巡检。
- `scripts/run_strategies.py`（现货策略主调度）：
  - 若策略被标记暂停新开仓，生成的 BUY 信号降级为 HOLD，SELL 信号正常放行。

### 3. 控制面与 UI 改造
**修改文件**：`pages/15_strategy_control_center.py`
- **读取逻辑**：
  在 `load_control_snapshot()` 中，获取当前顶部选中的租户 `tenant_id`，遍历策略列表时通过 `is_strategy_entries_paused(store, plugin.strategy_id, tenant_id)` 读取当前状态；
- **渲染组件**：
  在策略网格表格中新增 `st.column_config.CheckboxColumn`，列名 **「⏸️ 暂停新开仓 (只减仓)」**；
- **写入与审计**：
  用户修改并点击保存时，调用：
  ```python
  store.set_setting(f"strategy_entries_paused:{tenant_id}:{strategy_id}", "true" if new_val else "false")
  store.audit("STRATEGY_ENTRIES_PAUSED_CHANGED", {
      "strategy_id": strategy_id,
      "tenant_id": tenant_id,
      "entries_paused": new_val,
      "actor": "streamlit_admin",
  })
  ```
- **页面底部说明**：
  渲染 PRD 规定的三开关（单策略只减仓 vs 账户急停 vs 租户全停）对比卡片。

---

## 四、 风险分析、回滚方案与防故障机制

| 潜在风险 | 防御方案 |
| :--- | :--- |
| **误拦截平仓/止损导致爆仓** | 架构硬编码守卫：`side == BUY` 是拦截的前置必要条件，代码层彻底杜绝 SELL 被阻断。 |
| **多租户配置串扰** | 键名前缀强注入 `tenant_id`，UI 保存和执行器判定严格一致。 |
| **老旧配置键遗留冲突** | 统一生产解析函数提供严格的优先级回落与兼容，优先使用新键。 |

**回滚方案**：
本方案不涉及数据库表结构变更（仅读写 `runtime_config`），若出现任何非预期逻辑，直接代码回退（`git revert`），系统自动回落至原有调度逻辑。

---

## 五、 验收口径与测试矩阵

1. **UT1: 单策略开仓阻断与平仓放行（Reduce-Only 验证）**
   - 构造配置 `strategy_entries_paused:futu_real_master:volatility_long_gamma = true`；
   - 提交 BUY 订单：断言返回 `REJECTED`，断言记录 `STRATEGY_NEW_ENTRY_BLOCKED` 审计；
   - 提交 SELL 订单：断言顺利通过门禁并正常调用 broker 挂单。
2. **UT2: 租户隔离性验证**
   - 设置实盘 `futu_real_master` 开启暂停，模拟盘 `futu_paper_sandbox` 未开启；
   - 在实盘下提交 BUY：断言拦截；
   - 在模拟盘下提交 BUY：断言放行。
3. **UT3: 优先级回落测试**
   - 仅设置历史兼容键 `volatility_new_entries_paused = true` 时，解析函数能正确返回 `True`；设置新键后以新键为准。
4. **UI 冒烟**：
   - 在 `pages/15` 切换不同租户，表格勾选框显示各自独立状态；修改勾选并保存，数据库与审计流水准确生成。
