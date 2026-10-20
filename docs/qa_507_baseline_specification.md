# Tradeyi 项目：全量自动化回归测试体系与 507 绿灯基线报告

## 一、 测试体系总览
本工程采用 Python 3.12 + `pytest` 自动化测试框架，覆盖了量化交易系统的全生命周期关键链路：
- **实盘安全门禁（Real Trading Gate）**：交易确认码、单笔/日累计名义限额、审批指纹、多账户物理隔离白名单；
- **统一执行通道（Execution Channel）**：`submit_limit` 唯一通道、SHADOW 影子模式绝不碰券商、跨环境改撤单阻断；
- **期权与风控模型（Risk & Options）**：希腊字母 Greeks 动态风控、快速回吐保护、阶梯锁盈与追踪止损；
- **底层容错与对账（Reconciliation & Drill）**：券商持仓 ↔ 本地流水 ↔ 策略虚拟账本三账核验、断线故障演练与断言防御。

---

## 二、 507/507 绿灯基线达成记录
- **达成时间**：2026-10-20 20:20 (美股开盘前)
- **执行任务**：`pwsh-264`（Chief 验收）与 `pwsh-312`（Tester 独立复核）双重核验一致
- **全量用例数**：**507 项**
- **通过结果**：**507 passed in 166.53s (0:02:46)**，退出代码 `exit code: 0`

### 核心测试用例集分布
1. `tests/test_real_trading.py`：实盘门禁机制（5 项全部 PASS）
2. `tests/test_execution_safety_regressions.py`：A1/A2 安全回归（11 项全部 PASS）
3. `tests/test_execution.py`：订单生命周期与改撤单执行（35 项全部 PASS）
4. `tests/test_safety_and_strategies.py`：策略风控与限额拦截（12 项全部 PASS）
5. `tests/test_scheduled_task_board.py`：自动化调度与复盘投递（9 项全部 PASS）
6. `tests/test_review_advice.py`：复盘建议与诊断分析（2 项全部 PASS）
7. `tests/test_order_and_option_lifecycle.py`：到期实值期权兜底平仓（7 项全部 PASS）
8. `tests/test_shadow_acceptance_fees.py`：富途官方精准规费测算（9 项全部 PASS）
9. `tests/test_volatility_optimizer.py`：做多波动率分批止盈与奔跑（58 项全部 PASS）
10. `tests/test_combo_analytics.py` & `test_replay_and_auth.py`：组合装配与故障演练（16 项全部 PASS）

---

## 三、 测试执行环境铁律（Windows 沙箱避坑指南）
1. **禁用外部捕获管道**：在 DSH Windows 沙箱下运行 pytest 严禁带 `2>&1` 或 `| Select-Object` 重定向管道，否则会触发沙箱命名管道 EPERM 导致进程挂死；
2. **直连 Venv Python**：使用 `E:\Thisyi\trade\.venv\Scripts\python.exe -m pytest`，并显式指定 `--basetemp=data/qa/run_xxx`；
3. **后台异步跑全量**：全量 507 套件耗时约 2 分 45 秒，发起命令必须使用 `run_in_background: true`，依靠系统回调收口，严禁前台阻塞死等拖死交互。
