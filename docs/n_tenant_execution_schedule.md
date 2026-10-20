# Tradeyi 项目：通用多租户架构（N-Tenant）三阶段实施排期计划

## 一、 项目背景与演进目标
针对实盘真金交易（REAL）与前向推演模拟盘（SIMULATE）在运行过程中的解耦需求，以及未来横向扩展至 N 个实盘/模拟子账户（如主备实盘号、跨券商账户、专项赛马模拟仓）的机构级诉求，系统正式启动由“二元伴生模型”向“通用多租户架构（N-Tenant Architecture）”的全面演进。

---

## 二、 三阶段实施排期矩阵

| 实施阶段 | 阶段主题 | 核心交付物 | 责任角色 | 预计耗时 | 验收标准 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **阶段一** | **数据契约与账本联合主键强隔离** | `TradingStore` 联合主键支持、`ExecutionManager` 环境守卫、租户依赖注入、跨租户拦截单测套件 | Coder / Reviewer / Tester | 1 工作日 | 39 项隔离用例与安全测试全绿，实盘零停机兼容 | **已完成** |
| **阶段二** | **多租户独立守护进程与服务参数化** | `run_service.py --tenant <id>`、独立 pid 与文件锁、独立状态机追价/超时撤改补单、平滑灰度启动 | Coder / Architect / Ops | 1 工作日 | 模拟盘拥有对等自闭环状态机，双进程无资源争用 | **进行中（启动）** |
| **阶段三** | **全局多租户视窗与端到端闭环验收** | Streamlit 顶部多租户动态切换器、孤儿持仓侦测看门狗、全量 507+ 自动化回归报告 | Product / Coder / Tester | 0.5 工作日 | UI 严格按租户切片过滤，全量用例 100% 绿灯验收 | **待排期** |

---

## 三、 阶段二（进行中）详细任务拆解与分工

### 1. 服务启动参数化（@Coder）
- 重构 `scripts/run_service.py`，新增 `--tenant <tenant_id>` 命令行参数；
- 解析指定的租户 Profile，按租户身份初始化 `FutuPaperBroker`、`ExecutionManager` 与专属 `TradingStore` 实例；
- 独立管理服务单例锁文件：`data/trading-service-<tenant_id>.lock` 与 `.pid`。

### 2. 状态机对等与补单闭环（@Coder）
- 在模拟盘专属 Service Worker 中挂载完整的 `reprice_open_orders`（追价改单）与 `cancel_stale_orders`（超时撤改补单）轮询周期；
- 彻底根除模拟单超时撤单后无人管理的“孤儿单”技术隐患。

### 3. 运维平滑上线与进程托管（@Ops）
- 在保持生产实盘进程（PID 20692）不停机的前提下，以金丝雀方式拉起 `run_service.py --tenant sim_primary`；
- 巡检脚本自动识别并纳管多进程心跳与健康状态。

### 4. 质量与安全把关（@Reviewer & @Tester）
- 审查参数化改造代码，确保实盘默认参数向下兼容；
- 验证双进程并发读写 SQLite WAL 时的事务安全与锁竞争。
