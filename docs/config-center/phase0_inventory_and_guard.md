# 配置中心 Phase 0 盘点清单与守卫架构规范

> 状态：实施基线 (Phase 0 交付)  
> 适用工程：Tradeyi / Futu Quant (Streamlit + `run_service.py`)  
> 目标：盘点全量已有配置入口，确立唯一 Canonical Key 目录，建立 AST 静态防隐形配置守卫，实现配置零泄漏与透明化治理。

---

## 1. 架构原则与安全红线 (Phase 0 约束)

1. **单一权威与双向解析**：
   - 每一个配置项具备唯一的 **Canonical Key**（分层命名，如 `runtime.execution.mode`）；
   - 所有历史环境变量（如 `EXECUTION_MODE`）、代码别名（如 `execution_mode`）注册为 `aliases`，支持双向无歧义解析；
   - 严禁出现一个别名映射到两个不同规范键。
2. **零运行时行为变更 (Zero Runtime Impact)**：
   - Phase 0 仅提供只读元数据目录 `ConfigRegistry`，不引入任何数据库写入、不改动现有解析优先级；
   - 现有的 `租户 -> 账户 -> 策略 -> 全局` 解析链路完全保留，Registry 仅作为规范描述层。
3. **安全风险与权限分级**：
   - 所有涉真金 (`REAL_*`)、急停 (`kill_switch`)、执行模式 (`execution_mode`)、风控限额 (`RISK_*`) 标定为 `CRITICAL` 风险；
   - 敏感配置标记 `secret=True`，明文禁止入库与回显；
   - 高危项标记 `editable=False` 或 `requires_confirmation=True`，首阶段严禁开放无约束编辑。
4. **静态穷尽性与 CI 守卫防新增**：
   - CI 构建与测试套件中嵌入 AST 静态扫描守卫；
   - 扫描 `src/`、`scripts/`、`pages/`、`.env.example` 中的所有配置读取，任何未在 Registry 显式登记的新增配置一律阻断（测试变红）。

---

## 2. 扫描范围、豁免清单与合理理由

### 2.1 扫描范围 (Exhaustive Scan Scope)
- 源码目录：`src/futu_quant/**/*.py`
- 脚本入口：`scripts/**/*.py`
- 界面页面：`pages/**/*.py` 及 `app.py`
- 配置文件：`.env.example`

### 2.2 扫描提取 AST 语法模式
1. `os.getenv("KEY")` 及 `os.environ.get("KEY")`
2. `os.environ["KEY"]`
3. `Settings` 数据类字段（`Settings.__dataclass_fields__`）
4. `store.get_setting("KEY")` 与 `store.set_setting("KEY")` 中的静态字面量键

### 2.3 豁免清单 (Exemptions) 与合规理由
下列项经安全与架构联合审计，不属于量化系统业务与策略配置范畴，允许豁免：

| 豁免项 / 模式 | 分类 | 豁免理由 |
|---|---|---|
| `PATH`, `SYSTEMROOT`, `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `TEMP`, `TMP`, `USERPROFILE`, `HOMEPATH`, `SYSTEMDRIVE` | 操作系统环境变量 | Windows/Linux 标准系统路径与运行时临时目录，非交易业务配置 |
| `PYTHONUNBUFFERED`, `PYTHONUTF8`, `PYTHONPATH`, `PYTHONIOENCODING`, `MSYSTEM`, `TERM` | Python / 解释器配置 | 控制控制台输出编码、标准流与模块搜索路径，非交易系统业务参数 |
| `TRADEYI_ENV`, `TRADEYI_PROD_DB_PATH`, `TRADEYI_PROD_DB_OVERRIDE`, `TRADEYI_DEV_ROOT`, `TRADEYI_PROD_ROOT`, `TRADEYI_TEST_ROOT` | 测试沙箱与工作区分隔离 | 测试框架环境标记与 fail-closed 生产指纹守卫路径，禁止业务化修改 |
| `FUTU_OPEND_EXE`, `FUTU_OPEND_DIR`, `PROXIFIER_EXE` | 宿主机本地安装路径 | 依赖的外部第三方程序绝对安装位置，属机器环境层 |
| `DSH_HADOOP_USER`, `CUA_DRIVER_PIPE` | 容器/Harness 宿主变量 | DeepSeek Harness 平台底层注入参数 |
| `task_backoff_until:*`, `task_failure_count:*`, `last_run:*`, `last_attempt:*` 等 | 调度器临时运行态 | 数据库临时记录的瞬态重试与调度时间戳，属动态内部状态而非用户配置 |
| `volatility_signal_active:*`, `volatility_signal_z:*`, `volatility_activation_evidence:*` 等 | 雷达与信号瞬态 | 扫描器周期性刷新写入的标的活跃证据，属行情事件数据 |
| `symbol_blocked:*`, `strategy_reconciliation_disabled:*` | 风控熔断锁定态 | 由对账和三账守卫自动触发的运行时锁定标记，由自愈或解封接口驱动 |

---

## 3. 全量配置项分类目录概要 (367 项已登记)

- **① 运行环境与连接** (`runtime.*`): Futu OpenD 通信、市场 (US)、账户 ID、密码哈希等（极高风险，需重启生效）。
- **② 交易安全与实盘门禁** (`safety.*`): `REAL_TRADING_ENABLED`、白名单策略、单笔及单日限额、手工确认码等（极高风险，多道门禁保护）。
- **③ 急停与运行控制** (`control.*`): 全局急停 (`kill_switch`)、策略暂停 (`strategies_paused`)、故障自愈开关等（极高风险，即时生效）。
- **④ 风控边界** (`risk.*`): 组合杠杆、多标的 Delta/Gamma 配额、账户回撤阈值、预留现金比例等（极高风险，下一周期生效）。
- **⑤ 策略总览** (`strategy.*`): 策略启用、全局与租户级执行模式 (`SHADOW/PAPER/REAL`)、资本配额等。
- **⑥ Core Satellite** (`core.sat.*`): 选股配置、再平衡频率、SGOV 现金理财、NVDA 物理底仓保护等。
- **⑦ 期权与 Gamma** (`gamma.*`): 期权到期窗口、DTE、OI 门槛、行权风险退出规则等。
- **⑧ 波动率策略** (`volatility.*`): IV 百分位、效率阈值、金字塔加仓、棘轮止盈、提前收盘避险等。
- **⑨ 执行与订单** (`execution.*`): 报价步长、Tick 对齐、改单追价频率、滑点保护上限等。
- **⑩ 行情与数据** (`market.*` / `ml.*`): 快照保鲜期、日历节假日、ML 模型评分与阈值等。
- **⑪ 通知与可观测性** (`notification.*`): 飞书群 Webhook、告警白名单、事件级别等。
- **⑫ 运维与高级** (`ops.*`): 数据保留周期、备份、数据完整性巡检等。

---

## 4. CI 守卫与自动化回归

守卫测试位于 `tests/test_config_center_phase0_guard.py`：
1. **静态扫描与登记验证**：AST 遍历所有源码目录中的配置调用，断言所有非豁免项均已在 `GLOBAL_REGISTRY` 登记；
2. **单一权威与别名一致性**：断言 Registry 中无重复 canonical key，所有 aliases 均可双向解析；
3. **负向对照（必红测试）**：测试断言如果引入一个未登记的全新环境变量，扫描器必须抛出 `AssertionError`（真实可失败）。
