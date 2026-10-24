# 交易终端体验打磨与飞书通知深度人话化技术架构设计方案

> **设计文档**: docs/user_experience_and_feishu_humanization_design.md  
> **文档版本**: 1.0 (Architecture Design)  
> **责任人**: Architect (架构师)  
> **对齐需求**: docs/user_experience_and_feishu_humanization_prd.md (Product)  
> **状态**: 待 Chief 审批签字与计划排期

---

## 一、 架构设计原则与范围

1. **内外国界分明（Internal Enum vs External Presentation）**：
   - 系统内部数据流（DB 表、audit 事件、指标统计、Prometheus 指标）保持严格的英文字符串契约（如 `OPTION_LIFECYCLE_RISK`），保证代码可维护性与索引检索性能；
   - 外部呈现层（飞书推送卡片、Web 界面文案）通过**展示层映射引擎（Presentation Layer Mapping）**全量人话化，杜绝技术代号裸露。
2. **渐进增强与防故障（Fail-Safe）**：
   - 飞书卡片渲染遇到未登记的新增事件类型时，自动以安全人话模板回退，严禁抛异常阻断告警发送；
   - 动态 Host 探测若在无代理或非标准 Header 环境下，优雅回退至安全默认地址。
3. **风控单源可配与租户继承**：
   - 双层止损口径（相对 -25% + 绝对 $300）遵循生产统一配置解析链，界面直观呈现当前生效值与租户层级。

---

## 二、 模块详细设计与技术落地

### 模块一：飞书推送卡片四层人话化渲染引擎

#### 1. 核心映射引擎设计 (`src/futu_quant/feishu_humanize.py`)
新增独立的人话化格式化组件，封装全量事件中文字典与四层卡片构造逻辑：

```python
# 核心结构体契约
@dataclass
class HumanizedAlert:
    title: str          # 标题: Emoji + 等级 + 通俗中文事件
    what_happened: str  # 业务事实: 发生了什么 (标的、账户、具体金额)
    impact: str         # 资金/系统影响: 对交易员的具体影响与当前系统保护动作
    action_guide: str   # 行动指引: 建议下一步操作
    nav_url: str        # 直达后台页面锚点/链接
```

#### 2. 四层卡片模板渲染规则
- **Header（标题栏）**：
  根据 `level`（`CRITICAL` / `ERROR` / `WARNING` / `INFO` / `RECOVER`）动态组装色块与 Emoji：
  - `CRITICAL` / `ERROR` ➔ 🚨 / 🛑 `【紧急风控】`、`【系统异常】`；
  - `WARNING` ➔ ⚠️ `【风险预警】`；
  - `INFO` ➔ ℹ️ `【交易提醒】`；
  - `RECOVER` ➔ 🟢 `【自愈闭环】`。
- **Body（正文 Markdown）**：
  采用结构化段落分块展示：
  - **📌 业务事实**：提取 payload 中的 `symbol`、`account_id`、`notional`、`pnl` 等字段自动拼装为人话短句；
  - **⚡ 资金与系统影响**：明确标注“已自动阻断新开仓 / 持仓平仓不受影响”等确定性定心丸；
  - **👉 交易员行动指引**：给出直观指引（如“检查 OpenD 连接”或“建议在波动率控制台核验持仓”）。
- **Action（按钮交互）**：
  配置飞书 Interactive Card 的 `elements` 动作按钮，直达对应的 Streamlit 页面（如直达 `/risk_center` 或 `/volatility_options_strategy`）。

#### 3. 挂载位置
- 修改 `src/futu_quant/feishu_reporter.py` 中的 `send_card` / `_build_card_payload` 方法，调用 `humanize_feishu_alert(event_type, level, message, details)` 进行卡片构造。

---

### 模块二：双环境穿梭体验与 Hostname 动态自适应

#### 1. 动态主机名自适应解析机制
**修改位置**：`app.py`（侧边栏顶部环境切换挂载点）

- **技术痛点**：当前直接写死 `http://localhost:8601`，当用户通过内网 IP（如 `192.168.1.100`）或公网域名远程访问时，点击会因定向至设备自身 localhost 导致网络不可达。
- **实现方案**：
  利用 Streamlit 提供的客户端上下文 Headers 获取真实访问的主机地址：
  ```python
  from streamlit.web.server.websocket_headers import _get_websocket_headers
  headers = _get_websocket_headers() or {}
  host = headers.get("Host", "localhost:8501").split(":")[0]  # 提取纯 IP 或域名
  
  # 动态生成目标端口 URL
  target_port = 8601 if current_env == "prod" else 8501
  target_url = f"http://{host}:{target_port}"
  ```
  优雅回落：若无法获取 Headers，安全兜底为 `localhost`。

#### 2. 界面文案与视觉升级
- 替换裸露端口文案：
  - 生产环境侧边栏：显示 `[🧪 切换至仿真实验区]`，副标题提示 `基于独立测试数据库与进程`；
  - 测试环境侧边栏：显示 `[🏭 切换至生产正式环境]`；
- 彻底隐去 `(8501)`、`(8601)` 等研发底层端口标识。

---

###模块三：风控中心双层止损口径前端可视化

#### 1. 存储契约与统一解析
- **配置键**：
  - 相对止损比例：`volatility_stop_loss_pct:{tenant_id}`（默认 `-0.25`，全局回退 `volatility_stop_loss_pct`）；
  - 单笔绝对止损金额：`volatility_max_loss_notional:{tenant_id}`（默认 `300.0`，全局回退 `volatility_max_loss_notional`）。
- **解析函数**：统一在 `src/futu_quant/risk.py` / `volatility_roll.py` 中提供 `resolve_volatility_stop_loss_config(store, tenant_id)`，严格遵循租户级 > 全局级 > 默认值的层级。

#### 2. 界面交互与留痕 (`pages/7_risk_center.py`)
- 在风控中心新增「🎯 波动率策略双层止损口径」卡片；
- 提供数字输入框（比例输入框步长 1%，金额输入框步长 $50）；
- 管理员保存时：
  1. 调用 `store.set_setting` 写入当前选定租户键；
  2. 写入审计事件 `AUDIT_EVENT: STOP_LOSS_CONFIG_UPDATED`，记录操作人、租户、原值与新值；
- 卡片下方渲染 `st.dataframe`，展示最近 5 次该租户的风控变更留痕。

---

## 三、 风险评估与回滚方案

1. **飞书通知格式风险**：
   - 风险：若中文字典存在未覆盖事件或字段缺失，可能导致卡片渲染失败。
   - 防御：解析器采用字典 `get(event_type, default_template)`，未覆盖事件以通俗通用模板包装并打印原 message，绝对不吞告警、不报错阻断。
2. **Header 提取风险**：
   - 风险：不同反向代理（如 Nginx、Caddy）可能重写或丢失 Host Header。
   - 防御：代码采用三级安全回退：`headers.get("X-Forwarded-Host")` ➔ `headers.get("Host")` ➔ `localhost`。

**回滚方案**：
各模块均为表现层或轻量配置层改动，无底层表结构迁移。若需回滚直接通过 `git revert` 撤销对应前端页面与飞书渲染文件即可。

---

## 四、 验收口径与测试矩阵

1. **飞书告警人话化实测**：
   - 运行测试用例模拟发送 `DAILY_LOSS_KILL_SWITCH` 与 `OPTION_LIFECYCLE_RISK`；
   - 飞书群实收卡片核验：第一眼必须为中文业务标题，正文包含“发生了什么、对我的资金影响、去哪里处理”三要素，无生硬英文代码。
2. **环境穿梭自适应实测**：
   - 通过本机 IP（如 `127.0.0.1:8501`）与内网 IP 访问生产环境；
   - 点击「🧪 切换至仿真实验区」，验证浏览器准确跳转至对应 IP 的 8601 端口，无 404 或网络不可达。
3. **风控中心双层止损配置实测**：
   - 在 `pages/7` 修改实盘租户的单笔最大亏损为 `$350` 并保存；
   - 验证：数据库成功落盘 `volatility_max_loss_notional:futu_real_master`，留痕列表即刻显示本次修改记录；模拟盘租户不受影响。
