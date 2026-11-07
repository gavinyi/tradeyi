# 统一配置中心 PRD 与架构设计

> 文档版本：1.0  
> 状态：设计评审稿（可进入拆解实施）  
> 适用系统：Futu US 股票/期权量化交易系统（Streamlit + `run_service.py`）  
> 目标：让任何配置项都能被快速定位、理解、修改、验证和追溯，同时不破坏交易安全与服务稳定性。

---

## 1. 产品结论（先给答案）

当前配置并非真正“没有中心”，而是存在三类入口和两类存储：

1. `Settings` / `.env`：连接、交易环境、默认参数等静态配置；
2. SQLite `runtime_config`：策略、标的池、预算、复杂 JSON 配置；
3. SQLite `system_settings`：急停、暂停、运行状态、租户开关等 KV；
4. `pages/1_trading_config.py`、`pages/2_options_config.py`、策略高级页面：按业务局部展示配置；
5. 代码中的环境变量读取、默认值、策略常量：用户无法从页面发现。

**推荐方案不是立即把所有表物理合并，而是先建立“统一配置中心”逻辑层：**

- 一个配置目录（Registry）：登记所有可配置项的名称、分类、说明、类型、默认值、作用域、风险级别、来源、是否需要重启；
- 一个配置服务（ConfigService）：统一读、写、校验、权限、版本、审计和发布；
- 一个配置中心页面：统一搜索、分类浏览、查看有效值与来源、编辑、预览变更、发布和回滚；
- 底层暂时保留 `runtime_config` 与 `system_settings` 的读性能特征，由适配器按配置元数据路由；
- 新增配置只能通过 Registry + ConfigService，禁止页面或业务模块自行读写 `.env` / DB。

这是一条风险最小、可渐进迁移、符合交易系统安全要求的路线。

---

## 2. 背景与问题定义

### 2.1 用户问题

| 问题 | 现状 | 业务后果 |
|---|---|---|
| 找不到配置 | 配置分散在 `.env`、配置页、策略页、代码常量、DB 键 | 调参依赖开发者，容易改错 |
| 不知道作用 | 页面只显示字段，不显示被哪个策略/服务使用 | 修改后风险不可预知 |
| 不知道当前生效值 | 环境变量、DB、默认值可能存在覆盖关系 | “页面显示值”和“服务实际值”可能不同 |
| 修改不可追溯 | 现有部分 `system_settings` 没有完整 actor/history | 事故无法回答谁改了什么 |
| 修改无验证 | 有些参数可输入但组合后不合法 | 服务启动或交易周期才暴露问题 |
| 修改无发布语义 | 保存后立即生效，无法预览影响、回滚 | 交易时段误操作风险高 |
| 配置命名不一致 | `EXECUTION_MODE`、`execution_mode`、`strategy_execution_mode:*` 并存 | 用户无法通过关键词找到全部相关项 |

### 2.2 产品目标

- **30 秒找到**：输入自然语言、配置键、策略名或环境变量名，能找到相关项；
- **1 分钟理解**：看到中文说明、当前有效值、来源、作用域、影响范围、风险和示例；
- **2 分钟完成安全修改**：校验通过、预览 diff、确认发布、生成审计记录；
- **全链路可追溯**：谁、何时、从哪里、为什么、改前/改后、是否发布、是否生效；
- **交易安全优先**：REAL、急停、风控和保护类配置必须 fail-closed，不能被普通保存绕过；
- **渐进迁移**：不要求一次性修改数百个读写调用点，不影响现有生产运行。

### 2.3 非目标

- 第一阶段不重写策略逻辑；
- 第一阶段不物理合并 `runtime_config` 与 `system_settings`；
- 不把 Futu 密码、API Secret、Webhook Secret 等机密存入配置中心明文；
- 不允许配置中心成为绕过 `ExecutionManager.submit_limit()` 或 REAL 交易门禁的旁路；
- 不在交易时段默认自动发布高风险配置。

---

## 3. 配置分类体系（用户看到的“统一目录”）

分类必须按用户任务组织，而不是按代码文件组织。每个配置项只能有一个主分类，可有多个标签。

### 3.1 一级分类

| 分类 | 典型配置 | 默认风险 | 默认生效方式 |
|---|---|---:|---|
| ① 运行环境与连接 | 市场、OpenD host/port、账户、SIMULATE/REAL | 极高 | 重启/连接重建 |
| ② 交易安全与实盘门禁 | `REAL_TRADING_ENABLED`、允许策略、订单/日限额、手工确认 | 极高 | 需二次确认，部分重启 |
| ③ 急停与运行控制 | kill switch、全局暂停、策略启停、故障恢复开关 | 极高 | 即时 |
| ④ 风控边界 | 单笔名义、组合权重、Delta/Gamma/Vega/Theta、现金缓冲 | 极高 | 下一周期或即时 |
| ⑤ 策略总览 | 策略启用、执行模式、预算、策略级参数 | 高 | 下一周期 |
| ⑥ Core Satellite | profile、核心/卫星预算、再平衡频率、SGOV、标的池 | 高 | 下一周期 |
| ⑦ 期权与 Gamma | 标的池、到期、DTE、OI、价差、Greeks、退出规则 | 高 | 下一周期 |
| ⑧ 波动率策略 | 雷达预算、IV/效率阈值、加仓、止损、盈利保护、周末退出 | 高 | 下一周期 |
| ⑨ 执行与订单 | 初始报价、改单、超时、tick、组合腿执行、重试 | 高 | 新订单/下一轮 |
| ⑩ 行情与数据 | 快照新鲜度、缓存、数据源、市场日历 | 中高 | 下一周期/重启 |
| ⑪ 通知与可观测性 | 飞书通知、告警等级、心跳、日志级别 | 中 | 即时 |
| ⑫ 运维与高级 | 调度间隔、任务超时、备份、迁移、实验性功能 | 中高 | 视项而定 |

### 3.2 快捷视图

除分类外，页面必须提供以下快捷筛选：

- **实盘相关**：所有会影响 REAL 交易、账户、订单上限和风控的配置；
- **当前生效异常**：值非法、来源冲突、待发布、重启未生效；
- **最近修改**：最近 24 小时/7 天变更；
- **需要重启**：已保存但未加载；
- **本租户配置**：当前 tenant/account 作用域；
- **高风险配置**：`CRITICAL` / `HIGH`；
- **未迁移配置**：仍由旧页面/旧 API 直接维护的项目。

---

## 4. 配置项标准模型（Registry）

配置目录是代码中的显式注册表，不做成另一张不可审查的 DB 表。数据库只保存值、版本和历史。

### 4.1 `ConfigSpec` 字段

```python
@dataclass(frozen=True)
class ConfigSpec:
    key: str                         # 稳定 canonical key，例如 execution.global.mode
    aliases: tuple[str, ...]         # EXECUTION_MODE、execution_mode 等旧名
    category: str                    # 一级分类
    title: str                       # 中文名称
    description: str                 # 业务解释
    value_type: str                  # bool/int/float/enum/string/symbols/json/secret_ref
    default: object
    scope: str                       # global/tenant/account/strategy/symbol
    storage: str                      # runtime_config/system_settings/env/derived
    risk_level: str                  # LOW/MEDIUM/HIGH/CRITICAL
    editable: bool
    requires_admin: bool
    requires_confirmation: bool
    activation: str                  # immediate/next_cycle/restart/manual_release
    validator: str                   # 命名校验器
    dependencies: tuple[str, ...]
    consumers: tuple[str, ...]       # 模块、服务、页面
    tags: tuple[str, ...]
    secret: bool = False
    deprecated: bool = False
```

### 4.2 命名规范

新代码使用分层 canonical key：

```text
runtime.environment.market
runtime.execution.mode
safety.real.enabled
safety.real.max_order_notional
safety.real.max_daily_notional
control.kill_switch.global
control.strategy.volatility_long_gamma.enabled
risk.core_satellite.cash_buffer
strategy.volatility_long_gamma.entry.iv_percentile_min
execution.options.reprice.tick_policy
market.calendar.exchange
notification.feishu.enabled
```

旧键保留 aliases，迁移期间读写均可兼容，但页面只展示 canonical key。页面可同时展示“兼容旧键：`EXECUTION_MODE`”。

### 4.3 必须登记的元数据

每个配置项至少要回答：

1. 配置什么；
2. 为什么配置；
3. 谁能改；
4. 哪个服务使用；
5. 作用域是什么；
6. 何时生效；
7. 改大/改小有什么风险；
8. 合法范围和示例；
9. 与哪些配置存在约束；
10. 出错时系统如何 fail-closed。

未登记的配置键在 CI 守卫中报错，禁止新增“隐形配置”。

---

## 5. 有效值与来源模型

### 5.1 统一优先级

建议明确并固定以下优先级（从高到低）：

```text
紧急运行控制（DB system_settings）
> 已发布租户/账户/策略值（DB runtime_config）
> 环境变量/兼容 .env
> 代码默认值（Registry）
```

但优先级不是无限覆盖：

- REAL 相关安全门禁只能收紧，不能被低优先级来源放宽；
- secret 只允许引用环境变量/secret store，不允许页面读取明文；
- `derived` 值只读，由系统计算，不能直接编辑；
- 旧 `.env` 仅作为兼容迁移输入，不应成为 UI 保存后的权威来源。

### 5.2 页面展示

每个配置项显示一个“配置卡片”：

- 中文名称 + canonical key；
- 当前有效值；
- 来源徽标：`默认` / `.env` / `租户` / `账户` / `策略` / `急停`；
- 旧值与待发布值；
- 风险级别；
- 生效状态：`已生效`、`待下一周期`、`需重启`、`待人工发布`；
- 最后修改人、时间、原因；
- 使用者和影响范围；
- “查看历史”“查看依赖”“恢复默认”操作。

禁止只显示一个裸输入框而不展示来源和生效状态。

---

## 6. 页面 PRD

### 6.1 页面入口

将现有多个配置入口保留为策略快捷入口，同时新增一个一级导航：

> **配置中心**

配置中心是完整入口；旧页面改为“按场景编辑”的深链接视图，最终由同一套 ConfigService 保存。

### 6.2 页面布局

```text
┌──────────────────────────────────────────────────────────┐
│ 配置中心  [搜索配置、旧键、策略、功能……] [保存草稿]      │
├─────────────┬────────────────────────────────────────────┤
│ 分类导航     │ 当前分类 / 快捷视图                         │
│ 运行环境     │ [筛选] [风险] [作用域] [生效状态]           │
│ 实盘门禁     │                                                │
│ 急停控制     │ 配置卡片列表                                  │
│ 风控边界     │ 名称 · 有效值 · 来源 · 风险 · 生效状态        │
│ 策略总览     │ [编辑] [历史] [依赖]                           │
│ 执行与订单   │                                                │
│ ...          │ 右侧变更抽屉：前值 → 新值、影响、确认         │
└─────────────┴────────────────────────────────────────────┘
```

### 6.3 搜索体验

支持以下搜索：

- 中文名：`改单间隔`、`现金缓冲`；
- canonical key：`execution.options.reprice`；
- 旧环境变量：`EXEC_REPRICE_INTERVAL_SECONDS`；
- 策略名：`波动率`、`Core Satellite`；
- 标签：`实盘`、`高风险`、`需要重启`。

搜索结果必须同时显示分类、风险、当前值和快捷“编辑”。

### 6.4 编辑流程

1. 点击编辑，输入新值；
2. 前端即时校验格式和基础范围；
3. 服务端执行完整校验，包括跨字段约束；
4. 展示变更预览：旧值、新值、影响配置、影响策略、预计生效时间；
5. 填写变更原因（必填）；
6. 高风险项输入确认短语，例如 `确认修改 REAL 交易上限`；
7. 提交到版本化配置；
8. 根据 activation 规则立即应用、排队到下一周期或标记需重启；
9. 展示审计结果和生效状态。

### 6.5 高风险交互

- REAL 开关、账户、单笔/单日限额：不可批量修改，必须二次确认；
- kill switch：单独显著按钮，开启立即生效；关闭必须管理员 + 理由 + 当前健康检查通过；
- 风控边界放宽：需要展示“放宽幅度”和受影响策略；
- 交易时段修改高风险项：默认只允许保存草稿，禁止立即发布；
- 任意校验失败：整批不落库，不允许部分成功。

### 6.6 审计页

配置中心内置“变更历史”抽屉，并提供独立审计视图：

- 按人、键、时间、分类、风险、来源筛选；
- 显示改前/改后、reason、actor、版本、发布状态；
- 支持 CSV 导出；
- 对存量无 actor 记录明确显示“历史遗留·无法追溯”，不得伪造；
- 能查看版本 diff 和回滚目标。

---

## 7. 后端架构设计

### 7.1 分层

```text
Streamlit 配置中心 / 旧配置页
              ↓
        ConfigService
  (读、写、校验、权限、版本、审计、发布)
              ↓
      ConfigRegistry + Validators
              ↓
       StorageAdapter / Runtime Resolver
        ↙          ↓           ↘
 runtime_config  system_settings  env/.env
              ↓
       Settings / Strategy Consumers
```

### 7.2 核心 API

```python
class ConfigService:
    def list_specs(self, *, category=None, query=None, tags=()): ...
    def get(self, key, *, scope=None) -> EffectiveConfig: ...
    def get_many(self, keys, *, scope=None) -> dict[str, EffectiveConfig]: ...
    def validate(self, changes, *, scope, actor) -> ValidationReport: ...
    def preview(self, changes, *, scope, actor) -> ChangePreview: ...
    def save_draft(self, changes, *, scope, actor, reason): ...
    def publish(self, draft_id, *, actor, confirmation=None): ...
    def rollback(self, version_id, *, actor, reason, confirmation=None): ...
    def history(self, *, key=None, actor=None, from_=None, to=None): ...
```

业务代码不得直接调用 `set_setting` / `save_runtime_settings` 修改用户配置；过渡期可以由 ConfigService 内部调用适配器。

### 7.3 原子性与版本

- 一个页面的一次“保存”是一个变更批次；
- 所有变更先服务端校验，全部通过后在同一事务提交；
- 使用 optimistic locking：提交时带 `base_version`，版本不一致则拒绝并要求刷新；
- 发布写入配置值、版本历史、审计事件必须同一事务；
- 通知或缓存刷新失败不回滚已提交配置，但必须生成“配置已落库、应用未刷新”告警；
- 进程启动时打印配置指纹，不打印 secret 和账户敏感信息。

### 7.4 物理存储策略

**阶段一：双存储适配，不物理合并。**

- 高频简单状态继续走 `system_settings`，避免风控热路径被复杂 JSON/版本锁拖慢；
- 复杂配置继续走 `runtime_config`，使用已有版本和 `config_versions`；
- `.env` 变为兼容镜像/迁移输入，不再作为 UI 运行时权威；
- `system_settings` 增加 `setting_versions`；
- `audit_events` 增加独立 `actor` 列并建立索引；
- 新增配置必须在 Registry 声明 storage，禁止默认猜测。

**阶段二：逐项迁移。**

按风险和访问频率迁移：

1. `execution_mode`、策略启停、REAL 配置、risk limits；
2. Core Satellite、Gamma、Volatility 参数；
3. 通知、调度、数据和低风险参数；
4. 仅运行状态类 key 保留在热状态表，不进入配置历史。

**阶段三：收口旧入口。**

旧 helper 变为 deprecated wrapper，增加调用告警；最终 CI 禁止新增直接 DB/.env 写入。

---

## 8. 权限、审计与安全

### 8.1 权限矩阵

| 操作 | 普通查看 | 策略管理员 | 风控管理员 | 实盘管理员 |
|---|---:|---:|---:|---:|
| 查看低风险配置 | ✓ | ✓ | ✓ | ✓ |
| 编辑低风险配置 | - | ✓ | ✓ | ✓ |
| 编辑策略参数 | - | ✓ | ✓ | ✓ |
| 编辑风险边界 | - | - | ✓ | ✓ |
| 编辑 REAL/账户/交易限额 | - | - | - | ✓ |
| 开关 kill switch | - | - | ✓ | ✓ |
| 回滚配置版本 | - | 按权限 | 按权限 | ✓ |
| 导出审计记录 | - | ✓ | ✓ | ✓ |

当前系统只有管理员登录时，先实现“管理员 + 风险级别确认”模型；后续再接组织权限。

### 8.2 Actor 规范

新写入必须使用 `<class>:<id>`：

- `streamlit_admin:admin`；
- `service:reconciliation`；
- `scheduler:main`；
- `cli:migrate_config`；
- `test:pytest`。

空值、无冒号、空 class/id 必须拒写；未知但结构合法的 class 只警告。存量 payload 中的自由文本 actor 不回写、不伪造。

### 8.3 Secret 处理

- 页面只显示“已配置/未配置/末四位（如适用）”；
- 只存 `secret_ref`，实际值来自环境或受控 Secret Store；
- 历史 diff 不显示 secret 前值和后值；
- 禁止复制、导出、日志打印 secret。

---

## 9. 迁移实施计划

### Phase 0：盘点与守卫（1 个迭代）

- 建立 Registry 初始目录；
- 扫描环境变量、`Settings` 字段、`get_setting/set_setting`、`runtime_config` 键、页面输入项；
- 为每个配置项指定 canonical key 和 alias；
- CI 检查：新增配置必须登记；
- 输出未迁移清单，不修改运行逻辑。

交付物：配置清单、分类树、键映射表、风险分级表。

### Phase 1：统一服务与审计（1–2 个迭代）

- 实现 `ConfigSpec`、Registry、ConfigService、StorageAdapter；
- 完成 `setting_versions` 与 `audit_events.actor` 的幂等迁移；
- 实现统一读取和有效值解析；
- 将现有保存 helper 包装到 ConfigService；
- 完成单元测试、并发版本测试、actor 守卫。

### Phase 2：配置中心只读版（1 个迭代）

- 新增配置中心页面；
- 全量展示分类、搜索、有效值、来源、风险、使用方；
- 与旧页面对账，页面只读不改变生产配置；
- 验证“用户能找到配置”和“页面值等于运行时有效值”。

### Phase 3：低风险编辑（1 个迭代）

- 先开放通知、展示、低风险策略参数；
- 实现草稿、校验、发布、审计和回滚；
- 旧页面改为调用同一个 ConfigService；
- 观察一个完整交易周。

### Phase 4：高风险配置分批接入（2 个迭代）

顺序：策略启停 → 风控参数 → 执行参数 → REAL/账户配置。每一批必须包含：

- 生产前回归；
- 交易时段禁发策略；
- 变更前后配置指纹；
- 事故回滚演练；
- 代码审查和运维签字。

### Phase 5：旧入口收口

- 旧 helper 仅保留兼容读取；
- 直接 `.env` / DB 写入在 CI 中禁止新增；
- 页面统一跳转配置中心；
- 文档和交接手册更新。

---

## 10. 验收标准（Definition of Done）

### 产品验收

1. 搜索 `EXEC_REPRICE_INTERVAL_SECONDS` 能定位到“期权执行/改单间隔”，显示 canonical key 和当前有效值；
2. 搜索“现金缓冲”能定位到 Core Satellite 配置，并显示使用方、风险、作用域；
3. 用户能按“实盘相关”一次查看全部 REAL/账户/风控配置；
4. 修改后能看到旧值 → 新值、影响范围和生效时间；
5. 每个保存都必须填写原因，审计页能按 actor/key/time 查询；
6. 高风险配置不能由普通配置保存流程绕过确认；
7. 配置冲突、待重启、校验失败均有明确状态，不显示“已生效”假象。

### 技术验收

1. 所有新增配置项都有 `ConfigSpec`；
2. `ConfigService.get()` 返回值与 `Settings.load()`/策略运行时值一致；
3. 配置批次原子提交，版本冲突可检测；
4. 同值保存不新增无意义历史；
5. actor 格式非法时 fail-closed；
6. secret 永不出现在页面、审计、日志和导出；
7. 配置读取故障时，安全项采用保守默认或阻止交易，不能静默放宽；
8. 旧页面与新配置中心写入同一事实来源；
9. `ExecutionManager.submit_limit()`、REAL gate、kill switch 的安全链路不被改写或绕过；
10. 配置中心不可用时，交易服务仍按最后一个已验证版本运行；若无法取得安全配置，相关交易动作必须暂停。

### 性能验收

- 配置中心列表加载 P95 < 1 秒（不含外部行情）；
- 热路径读取不引入事务锁，单轮调度耗时劣化 < 5%；
- 审计查询最近 20 条 P95 < 500ms；
- 配置写入成功率 ≥ 99.9%，失败必须有可见错误和重试入口。

---

## 11. 自我审核（架构评审结论）

### 11.1 主要风险与处理

| 风险 | 审核结论 | 处理 |
|---|---|---|
| 追求“一张表”导致热路径性能下降 | 不接受 | 保留双存储，统一逻辑服务 |
| 页面做一套、服务做一套 | 不接受 | 页面只能调用 ConfigService |
| 只统一名称但没有来源/生效状态 | 不足 | 必须展示 effective value/source/activation |
| 一次性迁移所有调用点 | 高风险 | Registry + adapter 渐进迁移 |
| 允许页面直接改 `.env` | 不安全 | `.env` 仅兼容镜像，UI 不直接写 |
| 所有 DB 写入都记录历史造成噪声 | 不接受 | 区分配置写与运行状态写，热状态不进版本历史 |
| REAL 配置普通保存即可修改 | 不接受 | 权限、二次确认、交易时段限制、审计 |
| 配置发布后服务未刷新却显示成功 | 不接受 | 发布状态分为落库/已加载/已生效 |
| 回滚改变了安全边界 | 不接受 | 高风险回滚同样走权限和确认，必须记录原因 |
| 配置中心挂了交易服务就完全失控 | 不接受 | 服务使用最后已验证快照；安全配置不可用时 fail-closed |

### 11.2 发现的设计边界

1. **逻辑统一优先于物理统一**：当前系统的 `system_settings` 高频热读与 `runtime_config` 版本化 JSON 写入语义不同，强行合表会增加事故面；
2. **运行状态不是用户配置**：`last_run:*`、`task_backoff_until:*`、`symbol_blocked:*` 等必须可观测，但不应进入用户配置历史；
3. **默认值必须有唯一来源**：Registry 是文档化默认值，`Settings.load()` 逐步改为消费 Registry，避免“文档默认值”和“代码默认值”漂移；
4. **配置中心不是策略编辑器**：复杂 campaign JSON 可提供结构化编辑器和高级 JSON 模式，但必须经过同一套 schema/validator；
5. **保存与发布分离**：对于高风险项，保存草稿不等于生效，必须显式发布；
6. **兼容别名必须有期限**：每个 alias 要有 deprecate_at 和迁移 owner，避免永久维护三套名称。

### 11.3 最终架构结论

本方案通过“统一目录 + 统一服务 + 统一页面 + 双存储适配 + 统一审计”实现用户感知上的单一配置中心，同时保留交易系统需要的热路径性能、版本并发控制和 fail-closed 安全语义。

**评审结论：通过，建议按 Phase 0 → Phase 1 → Phase 2 顺序实施；未完成 Phase 1 的审计和版本能力前，不开放 REAL/执行/风控高风险配置的统一编辑。**
