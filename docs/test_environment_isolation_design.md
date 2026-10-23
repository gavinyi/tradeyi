# 测试环境隔离设计

> 状态：设计已定稿，待实施（**开盘窗口后再动**）
> 来源：用户 2026-10-23 指令 —— "把 test 环境正常隔离出来，把 git 的 test 分支也建起来，
> 在测试环境验证和开发，不要动生产环境。"
> 关联：`docs/fail_closed_design_rules.md`

## 1. 现状：生产与测试**同目录、同库、同锁**

| 资源 | 解析方式 | 位置 | 后果 |
| :--- | :--- | :--- | :--- |
| **数据库** | `store.py:39` `DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "trading.db"` | **源文件相对** | 换目录 → 自动换库 ✅ |
| **锁 / PID / stop** | `Path("data/...")`（相对路径） | **进程 CWD 相对** | 同 CWD → **共用锁**，单实例互斥 ❌ |
| **日志** | 相对路径 + `FUTU_LOG_ROLE` | CWD 相对 | 同 CWD → 日志互相覆盖 ❌ |
| **端口** | 8501（UI）/ 11111（OpenD） | 固定 | 同端口 → 起不来或抢端口 ❌ |
| **APPDATA** | 各入口显式重定向 | 已处理 ✅ | — |

**实测**：`data/trading-service.lock`、`data/volatility-radar.lock`、`data/*.pid` 都是
**CWD 相对**。若测试进程与生产同 CWD 启动，**单实例锁会直接互斥** ——
测试要么起不来，要么误判"已在运行"而静默跳过（**今晚雷达那次就是这类**）。

**结论**：**隔离的最小充分条件 = 换目录 + 换 CWD**。DB 会自动跟随（源文件相对），
锁/PID/日志会跟随 CWD。

## 2. 五条隔离轴

| 轴 | 机制 | 状态 |
| :--- | :--- | :--- |
| ① **代码目录** | 独立 git worktree / clone | 待建 |
| ② **数据库** | `DEFAULT_DB` 源文件相对 → 自动隔离 | ✅ 已有 |
| ③ **锁/PID/日志** | 独立 CWD（`cd` 到测试目录再启动） | 待规范 |
| ④ **端口** | UI 8501→8502；OpenD 11111→11112（若跑第二个 OpenD） | 待建 |
| ⑤ **环境标记** | `TRADEYI_ENV=test`，启动时断言不得连生产库 | 待建 |

## 3. 两级测试环境

### Tier 1：隔离开发/测试工作区（**立即做，也是本设计的目标**）

- 独立目录（git worktree 或 clone）+ 独立 CWD + 独立 `data/`
- **不跑实盘交易**，只跑：pytest、静态巡检、页面渲染检查、只读探针
- 用**桩 broker**（`tests/` 里已有大量 `FakeBroker`/`StubBroker`）或**模拟账户**
- **收益**：开发/验证不再可能碰到生产库 —— 今晚"测试写穿生产"那类事故从**结构上**不可能发生

### Tier 2：影子实盘（可选，未来）

- 第二个 OpenD 实例（不同端口 + 不同 APPDATA）+ 第二个券商账户
- 与生产**并行**跑，用于验证策略改动在真实行情下的行为
- **成本**：需要第二个 OpenD 与第二个账户，**不在本次范围**

## 4. git 分支策略（**2026-10-24 用户定案，已执行**）

| 分支 / 标签 | 用途 | 当前位置 |
| :--- | :--- | :--- |
| **`master`** | **正式基线 + 部署分支**（生产目录停在这里） | `2113260`（2026-10-23 生产运行态快照） |
| **`test`** | 开发 / 验证 / 测试（隔离区 `E:\Thisyi\trade-test` 的 worktree） | `c3627fa`（隔离守卫 + 测试修复，领先 master 1 个提交） |
| `main` | 仅作 `origin/main` 远端镜像，**不是部署基线** | `b7c1be7` |
| `archive/2026-09-windows-migration`（tag） | 旧 master（Windows 迁移基线）历史存档 | `00e01ee` |
| `snapshot/2026-10-23-production`（tag） | 今夜生产运行态的独立锚点 | `2113260` |

**部署流程（用户定案）**：开发在 `test` → 全量回归绿 + Reviewer 签字 + **用户批准** → 合入 `master` → 重启部署生产。

### 4.1 特性分支 + worktree 组合模型（**用户 2026-10-24 问「加特性分支还是用 worktree」——答案是两者组合**）

**分支是提交线，worktree 是工作目录，两者正交，不冲突。**

| 角色 | 目录 | 分支 |
| :--- | :--- | :--- |
| 生产 | `E:\Thisyi\trade` | `master`（只部署，不开发） |
| 集成测试（Tester 专用） | `E:\Thisyi\trade-test` | `test` |
| 需求开发（每个需求一个） | `E:\Thisyi\wt-<需求>` | `feature/<需求>`（从 `test` 切出） |

**流程**：`feature/<需求>` 开发 + 开发者自测 → 合回 `test` → **Tester 只在 `trade-test` 做全量回归 + 真机验证** → Reviewer 签 + 用户批准 → 合 `master` → 重启部署。

**硬规则**：
1. **Tester 不在 feature worktree 验**——feature 只做开发者自测；验收一律在 `test` 上做，保证「验的就是要合的那份」；
2. **同时最多 2 个 feature worktree**（串行纪律，避免并发改同一文件；超出的排队）；
3. **合并后立刻清理**：`git worktree remove E:\Thisyi\wt-<需求>` + `git branch -d feature/<需求>`，不留堆积；
4. **venv 复用**：所有 worktree 共用测试区 venv（`E:\Thisyi\trade-test\.venv`）跑 pytest（CWD=各自 worktree，`pythonpath=src` 按 CWD 解析）——不必每个 worktree 建一份 venv；
5. **feature 分支禁止直接改 `master`/`test` 指针**，只能经合并流程推进。

**硬规则**：
1. **生产目录（`E:\Thisyi\trade`）停在 `master`** —— 工作树被运行中的服务加载，**不得为了开发在此目录切分支**；代码部署 = `master` 前移 + 重启；
2. **`test` 只在测试目录里 checkout**，验证通过才允许合入 `master`；
3. **合并前必须通过**：隔离区全量回归 + 静态巡检 + `fail_closed_design_rules.md` §8 检查清单；
4. **禁止在服务运行期间切到不同 commit 的分支**（切同 commit 分支只改 `.git/HEAD`，不碰文件，安全）。

## 5. 落地步骤（**已执行，2026-10-24 00:41–01:04**）

1. ✅ 生产工作树 200 项改动中**白名单 124 文件 / 21,297 行**固化进 `master` 快照（排除 `data/`、临时产物、库文件）；
2. ✅ `git worktree add E:\Thisyi\trade-test test` 建独立工作目录；
3. ✅ 测试区独立 venv（`uv sync --extra dev --no-install-project` + pytest `pythonpath=src`）、独立 `data/`、独立锁；
4. ✅ `TRADEYI_ENV=test` + `TRADEYI_PROD_DB_PATH` 守卫（`tests/conftest.py`，缺失即 fail-closed）；生产指纹会话守卫在测试区指向**真生产库**；
5. ✅ 隔离区全量回归 **959 passed / 20 skipped / 0 failed（349.77s）**，生产 `runtime_config` 指纹前后一致。
6. UI 端口 8502 与 `data/qa` 独立目录：测试区按需起 UI 时使用（本轮未起 UI）。

## 6. 验收

1. 在 `trade-test/` 里跑全量 pytest，**生产 `data/trading.db` 的 `version` 与 sha 前后不变**；
2. 测试目录内启动服务，**不与生产的锁/PID 冲突**（两个 `.lock` 各自独立）；
3. `TRADEYI_ENV=test` 下尝试连生产库 → **拒绝启动并给出可读错误**；
4. 生产目录 `git status` 分支仍为 `main`，工作树未被测试操作改动。

---

## 附：用户问的"一键暂停新开仓、不影响旧持仓"开关 —— **答案是：没有**

**代码里已有这个语义的键**：`volatility_new_entries_paused:{tenant_id}`
（`run_gamma_strategy.py:80-84`，租户键优先、全局兜底）—— **只停新开仓，不影响退出管理**。

**但页面上零入口**：`pages/` 全目录**没有任何**对它的读写。
页面唯一的开关是 `pages/15_strategy_control_center.py:216-248` 的 **`strategies_paused`** ——
而它会**连持仓的止盈/止损/到期退出一起停**（10-23 已把文案改成明示这一点，并指向"暂停新开仓"）。

**结论**：**需要新做一个页面开关**，写 `volatility_new_entries_paused:{tenant_id}`。
**记入待办，明天做**（用户已确认）。
