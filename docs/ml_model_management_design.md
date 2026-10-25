# ML 模型管理功能 —— 技术设计

> 状态：设计已定稿，待实施（按 Product 的四步顺序）
> 上游：Product 的《ML 模型管理功能规划》
> 关联：`docs/fail_closed_design_rules.md`、`docs/risk_limits_tenant_scoping_design.md`、`docs/tenant_key_inventory.md`

## 1. 现状：三层都断了（实测）

| 层 | 事实 | 证据 |
| :--- | :--- | :--- |
| **调度** | Windows 计划任务丢失 | `schtasks` 查无 `TradeFuto-ML-Monthly-Retrain` |
| **依赖** | `sklearn` 从未安装 | `pyproject.toml` 有 `[ml]` extra，但 `import sklearn` → `ModuleNotFoundError` |
| **可见性** | 无心跳、无告警、无页面 | `run_service.py` 无任何 ML 任务 |
| **模型** | 生产 pkl 停在 2026-09-09 | `data/ml_models/volatility_scorer_v3.pkl` |
| **陷阱** | 曾经的「月度重训」脚本 **只刷元数据、不训练**（**已于 2026-10-25 P1-05 删除**） | 它会 `shutil.copy2` 备份 pkl 并写 `v3.json`，**从不调用训练器** —— 这就是 json 被刷新、pkl 没动的原因 |

**影响**：`volatility_roll.py:317` 的 `MLSymbolScorer` 加载失败 → **降级成规则打分**（`ml_symbol_scorer.py:68-75`），不崩、不拦单，但**评分质量静默退化约 6 周**。

---

## 2. 参数存储与解析（回答 Product 的问题）

### 2.1 分类口径

**沿用 Product 自定的切分法**：**影响"选什么标的 / 下多大注"的按租户；纯工程/市场常量全局。**

| 参数 | 归类 | 键 | 理由 |
| :--- | :--- | :--- | :--- |
| `scoring_mode`（ML/RULE） | **按租户** | `ml_scoring:{tenant}` | 策略行为；实盘 ML / 模拟规则才能做对照 |
| `auc_guard` | **按租户** | 同上 | 决定候选是否替换 = 影响选标的 |
| `min_samples` | **按租户** | 同上 | 同上 |
| `train_window_days` | **按租户** | 同上 | 同上 |
| `feature_flags` | **按租户** | 同上 | 同上 |
| `ml_threshold` | **按租户** | 同上 | 决定入选门槛 |
| `model_path` | **按租户可覆盖** | 同上 | 实盘/模拟可用不同模型文件 |
| `enabled_tenants` | **按租户** | 同上 | 哪些租户启用 ML |
| **`min_improvement_margin`** | **按租户** | 同上 | **门控「相对改进」的一部分**（默认 0.01）——样本 ~300 时 AUC 差 0.01 在噪声内，无此边际会把统计上无法区分的模型推上生产 |
| 训练超参（`n_estimators` 等） | **全局** | env / 训练脚本 | 工程常量，不随账户变 |
| 特征列定义（`feature_cols`） | **全局** | 由模型文件自带 | 由模型结构决定 |
| 数据路径、备份目录 | **全局** | env | 工程常量 |

> **因子计数：9 项**（按租户/租户可覆盖）—— `scoring_mode`、`auc_guard`、
> `min_improvement_margin`、`min_samples`、`train_window_days`、`feature_flags`、
> `ml_threshold`、`model_path`、`enabled_tenants`。
> **与 PRD 区块③ 必须同数同名**（当前 PRD 已补齐 `ml_threshold` / `model_path`）。
> **「重训频率」不在此表** —— 它属**调度配置**（`run_service` 的 interval），不是打分配置。

### 2.2 三层解析链 —— **与 P0-7 完全同构，不新开第二套**

```
① real_account_config:{account_id} 的 ml 段      ← 账户级，最具体
② ml_scoring:{tenant_id}                          ← 租户级
③ env 全局默认（ML_* 环境变量）                    ← 仅兜底
```

**规则（逐条复用 P0-7 的既定口径）**：
1. **部分配置即拒绝**：账户级存在任一参数就必须齐全，缺项 → 拒绝打分 + ERROR，**不得用 env 静默补齐**；
2. **回落必须可观测**：账户级全缺 → 回落 env 记 WARN + 计数；
3. **缓存键含 (账户, 交易日)**，配置改动后立即重解析；
4. **解析失败抛异常**，不返回默认值继续打分。

> **单一入口**：新增 `resolve_ml_scoring_config(store, broker)`，与 `resolve_risk_limits` **同一个模式、同一个模块族**。
> 禁止在 `MLSymbolScorer` 里裸读 `os.getenv`（现状 `:48-54` 就是这样，必须改）。

### 2.4 配置留痕：谁改的、何时改的（Product 追加要求）

**现状实测 —— 留痕能力取决于写在哪张表，而且两张表能力不同**：

| 写入路径 | 表 | 留痕 |
| :--- | :--- | :--- |
| `mutate_runtime_config(key, default, updater, actor=...)` | `runtime_config` | ✅ 调 `record_config_version` → `config_versions(config_key, value_json, **actor**, **reason**, created_at)` |
| `set_setting(key, value)` | `system_settings` | ❌ **无 actor、无历史** |
| `store.audit(event_type, payload, strategy_id)` | `audit_events` | ⚠️ **无 actor 列**（`store.py:292-298` 只有 event_type/strategy_id/payload_json/created_at）—— actor 只能塞进 payload，**无法按人查询** |

**规则**：
1. **ML 配置写 `runtime_config`**（走 `mutate_runtime_config`），**不写 `system_settings`** ——
   这样才能自动获得 `config_versions` 的 **actor + reason + 时间** 留痕；
2. **每次改动必须显式传 `actor`**（页面操作传 `streamlit_admin:<user>`，调度传 `service`），
   **禁止用默认值 `"system"`** —— 否则留痕等于没有；
3. **`reason` 必填**（"为什么改"），空 reason 视为无效；
4. **建议给 `audit_events` 加 `actor` 列**（一次性迁移），
   否则审计事件只能靠 payload 里的字段，**无法按人检索**；
5. **页面配置区每次保存后立即显示最近 5 条变更**（谁/何时/改了什么）。

> **这条与 `tenant_key_inventory.md` §H 是同一个根**：同一类配置分住两张表、两套 API、
> 两种留痕能力。**ML 配置不要再去加剧它 —— 直接走有留痕的那条路。**

### 2.5 模型文件
- 现状：单文件 `data/ml_models/volatility_scorer_v3.pkl`。
- 目标：**每租户一个文件**（`volatility_scorer_v3.{tenant_id}.pkl`），全局那个作为兜底。
- **替换必须原子**：训练产物写 `*.candidate.pkl` → 校验 AUC → **原子 rename** 覆盖；失败不动原文件。

---

## 3. 调度接入与 subprocess 边界（回答 Product 的问题）

### 3.1 单一入口

- 纳入 `run_service` 统一调度：`due(store, "last_run:ml_retrain", interval, now)`；
- **不再装 Windows 计划任务**（双入口必然双跑）；
- 训练**不在服务主循环里跑** —— `subprocess` 分离，避免阻塞交易任务。

### 3.2 subprocess 契约

```python
subprocess.run(
    [sys.executable, str(ROOT / "scripts/ml_retrain_monthly.py"), *args],
    capture_output=True, text=True,
    timeout=float(os.getenv("ML_RETRAIN_TIMEOUT_SECONDS", "1800")),
    cwd=str(ROOT),
)
```

| 边界 | 规则 |
| :--- | :--- |
| **超时** | 默认 30 分钟；超时 → kill + ERROR + 告警 |
| **并发锁** | **必须**：`data/ml_models/.retrain.lock` 文件锁 **或** `store` 里的 `ml_retrain_running` 键（带 PID + 时间戳，**超时自动失效**防僵死） |
| **失败重试** | **不自动重试** —— 训练失败多为数据问题，重试无益；记 ERROR + 告警 |
| **心跳** | `ml_retrain` 心跳：`RUNNING` → `HEALTHY` / `ERROR`（纳入监控页） |
| **日志** | stdout/stderr 落 `data/logs/ml_retrain.log`（**不是** `futu-logs`） |
| **退出码** | 0=REPLACED / 1=SKIP（AUC 不足或样本不足）/ 2=FAILED —— **SKIP 不是错误**，不告警 |

### 3.3 AUC 门控（已有，保持）

`ml_retrain_monthly.py:35` `AUC_GUARD = 0.50`；新模型 AUC 不低于当前才替换，样本 <200 则 SKIP。
**门控阈值改为按租户可配**（见 §2.1），默认值不变。

---

## 4. 新鲜度守卫（"断了没人知道"的根治）

### 4.1 判据必须取 **pkl 内的 `trained_at`**，回落 pkl mtime

**🔴 json 的 `trained_at` 是陷阱（Coder 实测）**：`volatility_scorer_v3.json:3` 的
`trained_at: 2026-10-14` 是那个**已删除的假重训脚本**写的**元数据刷新时间**，
**不是训练时间**。而 pkl 本体 mtime 是**更老**的那个（实测比 json 老一个月以上）。

**若守卫读 json → 永远显示"很新" → 永远绿 → 形同虚设。**

**规则**：
1. 判据 = **pkl 内的 `trained_at`**（pkl 确实带这个键），**回落 pkl mtime**；
2. **json 那个字段必须改名或标注**为 `metadata_refreshed_at` ——
   **名字骗人比代码错更难查**（今晚已栽过一次：那个**已删除的假重训脚本**冒充训练器）；
3. 守卫必须**在判据自身上可做变异测试**：构造 pkl 的 `trained_at` 为 **N 天前**
   （**N 相对生成、不写死**）→ 必须转红；
4. **关键判据**：**json 与 pkl 不一致时，必须显示更老那个** ——
   这是"不读 json"的唯一可证明形式（PRD 验收 #2 就是这条）。

### 4.2 告警必须走 `alerts.send` + 白名单 + `delivered` 校验

**两档阈值（与 `ml_freshness.py` 的 `DEFAULT_WARN_DAYS` / `DEFAULT_STALE_DAYS` 对齐）**：

| 年龄 | 级别 | 文案 |
| :--- | :--- | :--- |
| **≥ 30 天** | 🟠 **WARN** | 「建议重训」 |
| **≥ 35 天** | 🔴 **ERROR** + 飞书告警 + 心跳 | 「必须重训」 |

**为什么两档**：**30 天只是"该关注"**（WARN 不扰民），**35 天是"必须处理"**（ERROR 要人知道）——
**一刀切 ERROR 会训练用户忽略告警，告警疲劳比没有告警更糟。** 阈值按租户可配。

- 🔴 **不能只写 `operational_events`**。这正是 P1-1 的教训（`HARD_STOP_LIMIT_INVALID`
  只写一张表、`delivered=0`，"库里有、用户看不到"）。

---

## 4a. 静默降级必须变成**显式状态**（Coder 找到的机制）

**根因（实测）**：`ml_scorer.py:54-62` —— `pickle.load` 抛异常 →
**`logger.info(...)`** → `return BuiltinRuleModel(), default_cols`。
**不抛异常、不上报、INFO 级**。

于是 `ml_symbol_scorer.py:66-75` 的 `except` **永远不触发**（异常被上游吞掉了）
→ `scorer.enabled` **恒为 `True`** → **从外面看"ML 是启用的"，实际跑的是规则模型**。
**这就是"降级 6 周没人知道"的完整机制。**

**修法**：
1. **回退是显式状态**：`scorer.enabled = False` + **记录降级原因**
   （缺依赖 / 加载失败 / 模型不兼容 / 特征不匹配）；
2. **降级原因必须可查**（心跳 + 状态卡）；
3. **上报告警**：`alerts.send` + 白名单 + `delivered` 校验（同 §4.2）；
4. 页面状态卡显示 **"ML 打分" / ⚠️ "规则降级"** —— **降级不拦单（正确），但必须让人一眼看见**。

---

## 4b. AUC 门控当前**永远拦住替换**（Coder 发现，我复核后的精确失效模式）

**实测代码路径**（`ml_retrain_monthly.py:94-105`）：
- `load_metrics` 读 json → json **无 `auc` 键**（`volatility_scorer_v3.json` 实测确认）→ 走 pkl 分支；
- `:103` `float(data.get("metrics", {}).get("auc", 0))` —— pkl **无 `metrics` 键** →
  **返回 `0.0`（不是 `None`）**；
- `:121` `if new_auc < AUC_GUARD:` → **`0.0 < 0.50` 恒真** → **"不替换"分支永远命中**。

> **修正 Product 的表述**：后果不是"可能用更差的模型换掉生产模型"，而是
> **"永远不替换、自动重训永远不生效"**。**这更安全，但同样是静默失效** ——
> 日志里有一行"低于安全阈值"，但**没有告警、没人看日志**，
> 与"降级 6 周"是同一个形态。

**修法**：
1. `load_auc` 从 pkl 的 `metrics.auc` / 顶层 `auc` 读；**两者都缺 → 返回 `None`（不是 0.0）**；
2. `None` → **SKIP + 告警**，**不静默放行**（`:118` 已有 SKIP 分支，但触发不到）；
3. `AUC_GUARD = 0.50` **必须改成有意义的值**，或**显式标注"默认门控极弱，需人工设定"**；
4. **这条是第四步（自动月度重训）的硬阻塞** —— 修好并人工验收前，不得开启自动重训。

### 4b.1 门控值建议与 SKIP 的可观测性

**门控值建议**（两层，缺一层就少一道保护）：

| 层 | 判据 | 建议值 |
| :--- | :--- | :--- |
| **绝对下限** | `new_auc < AUC_GUARD` → SKIP | **0.55**（0.50 = 抛硬币，等于没有门控） |
| **相对改进** | `new_auc < old_auc` → SKIP | **必须优于当前**（现有逻辑） |
| **当前 AUC 未知** | `old_auc is None` | **SKIP + 告警** —— **不允许在不知道基线的情况下换模型** |

> 相对改进是**真正的那道保护**；绝对下限只是兜底。**两者都要。**

**🔴 两层是「与」关系，缺一不可**（Product 强调）：
- **只有绝对下限** → **"缓慢退化"会通过**（每月比上月差一点点，但都 >0.55）；
- **只有相对改进** → **"整体都很差"会通过**（新模型 0.51 优于旧的 0.50，但两者都是垃圾）。
- **替换条件**：`new_auc >= AUC_GUARD` **且** `new_auc > old_auc`（`old_auc` 已知时）。

**🔴 相对改进还需要「最小边际」**：样本量 ~300 时，AUC 差 0.01 **完全在噪声范围内**。
没有边际的门控会被噪声反复触发，把**统计上无法区分的模型**推上生产。
**建议**：`new_auc >= old_auc + min_improvement_margin`（默认 **0.01**，按租户可配）——
**边际也应可配**，理由同上。

**门控值按租户可配**（`auc_guard` / `min_samples` / `train_window_days`，见 §2.1）——
**实盘配得严、模拟盘配得松**，正好**对照"严门控 vs 松门控"的模型质量差异**。
默认值：`AUC_GUARD = 0.55` + **必须优于当前**，**两者同时满足才替换**。

**🔴 SKIP 必须可观测，否则会重演同一个病**：
修好门控后，如果门控设得太严 → **每个月都 SKIP** → 模型永远不更新 → **又是一个"静默不生效"**。

**规则**：
- `ml_retrain_history` 记录每次 SKIP（**含原因**：`AUC_BELOW_FLOOR` / `AUC_NOT_IMPROVED` / `AUC_UNKNOWN` / `INSUFFICIENT_SAMPLES`）；
- **连续 SKIP 计数**上页面 + 心跳；
- **连续 ≥2 次 SKIP → 告警**（"重训连续失败，模型未更新"）——
  **SKIP 不告警是对的，但"一直在 SKIP"必须告警。**

---

## 5. 页面结构（`pages/37_ml_model_center.py`）

| 区块 | 内容 | 数据来源 |
| :--- | :--- | :--- |
| **① 状态卡** | 版本 / `trained_at` / **年龄（天）** / AUC / 样本数 / **打分方式（ML 或 ⚠️规则降级）** / 新鲜度 🟢🟠🔴 | `data/ml_models/*.json` + 打分方式 |
| **② 重训控制** | 手动触发（**管理员 + 二次确认**）/ 重训历史表（**含 SKIP —— 留痕可见但不告警**）/ **AUC 门控可视化** | 新表 `ml_retrain_history` |
| **②b SKIP 原因列** | 重训历史表**必须显示 SKIP 原因**（`AUC_BELOW_FLOOR` / `AUC_NOT_IMPROVED` / `AUC_UNKNOWN` / `INSUFFICIENT_SAMPLES`）—— **只写日志等于没写** | 同上 |
| **③ 配置区** | §2.1 的按租户因子，逐项可改 + **留痕审计**（§2.4）+ **保存后显示最近 5 条变更** | `ml_scoring:{tenant}` 段（写 `runtime_config`） |
| **④ 对照视图** | 实盘(ML) vs 模拟(规则) 的评分分布、入选标的差异 | 打分日志 |

> **静默降级必须常驻可见**（Product 要求 2）：状态卡显示 **"ML 打分" / ⚠️ "规则降级"**，
> 且降级时**同时**：ERROR 告警 + 心跳 + 页面红色指示。
> **降级本身不拦单（正确），但必须让人一眼看见** —— 这是"静默降级 6 周没人知道"的直接对策。

**新表 `ml_retrain_history`**（幂等建，纳入 `store.initialize()`）：
`id, tenant_id, started_at, finished_at, status(REPLACED/SKIP/FAILED), old_auc, new_auc, samples, duration_s, model_version, error`

---

## 6. 防陷阱（必须做）

1. **删除「假重训」脚本**（P1-05，2026-10-25 已完成）—— 它只刷元数据、不训练，名字却像训练器，
   **已是实测陷阱**（json 被刷新、pkl 没动，误导了 6 周）；
2. **`[ml]` extra 必须真装**（`uv sync --extra ml`），且**启动时校验**：
   缺 `sklearn` → **告警 + 明确降级提示**，不静默；
3. **`MLSymbolScorer` 不再裸读 env**（`:48-54`），改走 `resolve_ml_scoring_config`；
4. **降级路径必须留痕** —— 现在的 `logger.warning` 不够（没人看日志）。

---

## 7. 落地顺序（Product 修订版，同意）

| 步 | 内容 | 行为变更 | 依赖 |
| :--- | :--- | :--- | :--- |
| **1a** | 回退显式化（§4a）+ 新鲜度守卫读 **pkl**（§4.1）+ **只读状态卡** | ❌ **无** | 无 —— 可先做 |
| **前置** | **修 AUC 门控**（§4b） | ❌ 无 | 第四步的硬阻塞 |
| **1b** | 装 `sklearn` + **受控重启** + 验证 ML 打分生效 | ✅ **有**（打分方式从规则切到 ML） | **需用户签字** |
| **2** | 手动触发一次重训 + **人工验收** | ✅ 有（可能替换模型） | AUC 门控已修 |
| **3** | 配置区 + 对照视图 | 视配置而定 | §2 三层解析 |
| **4** | **自动月度重训** + 纳入调度 | ✅ **有**（策略行为变更） | **需用户明确拍板** |

**硬约束**：
1. **1a 必须先于 1b**，1b 必须先于第 4 步；
2. **`uv sync --extra ml` 不是低风险步骤** —— 它会让**下次重启**从"规则打分"切成"真 ML 打分"，
   **必须与「验证 + 签字 + 受控重启」绑在一起**；
3. **第 4 步需用户明确拍板**，且必须先完成第 2 步的人工验收。

---

## 8. 验收（交给 Tester）

1. 模型年龄 >35 天 → **ERROR 告警且 `delivered=1`**（不是只写 `operational_events`）；
2. 手动触发重训：并发两次 → **第二次被锁挡住**；
3. 训练超时 → kill + ERROR + 告警；
4. AUC 低于门控 → **SKIP，不替换生产模型**，且**不告警**（SKIP 不是错误）；
5. `scoring_mode=RULE` 的租户 → 打分走规则，**且页面显示"规则降级"**；
6. 账户级配置缺项 → **拒绝打分 + ERROR**（不静默用 env）；
7. 缺 `sklearn` → **启动告警 + 明确降级提示**（不静默）；
8. **任何一条告警都必须能在页面上看到投递状态**（`delivered` 字段）。
