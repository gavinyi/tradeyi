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

### 4.1 分支模型 —— **行业标准：GitHub Flow + 受保护分支 + 环境分支（test 作 staging gate）**

> 用户 2026-10-24 定案：「只保留一个 test 目录，需求从 master 切出，分支之间不能相互上」+「用行业的标准」。
> 对应的行业标准就是 **GitHub Flow（短生命周期特性分支 + 受保护主干）** 与 **环境分支（把 `test` 当 staging 门）** 的组合。

| 角色 | 目录 | 分支 | 行业对应 |
| :--- | :--- | :--- | :--- |
| 生产基线（受保护） | `E:\Thisyi\trade` | `master` | protected main / production |
| 预发验证门（受保护） | `E:\Thisyi\trade-test` | `test` | staging / pre-production |
| 需求开发（短生命周期） | 同上目录临时切 | `feature/<需求>`（**从 `master` 切**） | short-lived feature branch |

**流程（标准 trunk→staging→production）**：
`master` → 切 `feature/<需求>` → 开发 + 自测 → **合入 `test`**（staging）→ Tester 全量回归 + 真机验证 → Reviewer 签 + **用户批准** → **合入 `master`** → 打 release tag → 重启部署。

**行业标准规则（逐条落地）**：
1. **`master` / `test` 都是受保护分支**：**禁止直接 commit / force-push**；只能经合并进入（单人 + Agent 团队里，等效 PR 审阅 = Reviewer 签 + Chief 执行合并）；
2. **特性分支短生命周期**：一个需求一条 `feature/<需求>`，**从 `master` 切出**，合并后立即删除；
3. **单向流动**：`feature → test → master`，**禁止 feature 互合、禁止 feature 直进 master、禁止 master 回灌 feature**（要新基线就 rebase 到 master 后继续）；
4. **合并策略**：`feature → test` 用 **squash merge**（保持 test 历史干净）；`test → master` 用 **fast-forward / squash**，master 保持线性、每个 commit 可部署；
5. **每次 master 前移 = 一个可部署版本**，打 **CalVer release tag**（`v2026.10.24-1`）并记录变更；**回滚 = 回退到上一个 release tag + 重启**；
6. **合并前必须绿**：隔离区全量回归 + 静态巡检 + `fail_closed_design_rules.md` §8 清单；
7. **同一时刻 `trade-test` 只跑一条线**（Tester 验证期间开发不得占用），串行纪律；
8. **venv（单测试目录模型）**：只用 `trade-test\.venv`（`--no-install-project`，**无项目安装**）；**禁止从任何非生产目录使用 `E:\Thisyi\trade\.venv`**（该 venv 的 `__editable__.futu_paper_quant-0.1.0.pth` 指向生产 `src`）；**共享 venv 上永不执行 `uv sync`**（sync-to-lock 会卸掉 test 线依赖）；feature 确需新依赖 → 该 feature 自带 venv。

**首个 release 动作（收盘后、批准后执行）**：`test → master` 合入隔离守卫提交，打 `v2026.10.24-1`。

**硬规则**：
1. **生产目录（`E:\Thisyi\trade`）停在 `master`** —— 工作树被运行中的服务加载，**不得为了开发在此目录切分支**；代码部署 = `master` 前移 + 重启；
2. **`test` 只在测试目录里 checkout**，验证通过才允许合入 `master`；
3. **合并前必须通过**：隔离区全量回归 + 静态巡检 + `fail_closed_design_rules.md` §8 检查清单；
4. **禁止在服务运行期间切到不同 commit 的分支**（切同 commit 分支只改 `.git/HEAD`，不碰文件，安全）。

### 4.2 venv 策略（**单测试目录模型，2026-10-24 Reviewer 实测精确化**）

**危险源不是"共享 venv"，是"生产 venv"——它带着指向生产 src 的项目安装：**

```
E:\Thisyi\trade\.venv      → __editable__.futu_paper_quant-0.1.0.pth → E:\Thisyi\trade\src   🔴
E:\Thisyi\trade-test\.venv → 只有 _virtualenv.pth（virtualenv 自带），无项目安装              ✅
```

| 场景 | 解析到 | 结论 |
| :--- | :--- | :--- |
| `trade-test` 里 pytest（`pythonpath=["src"]` 按 CWD） | 本地 `trade-test\src` | ✅ 正确 |
| `trade-test` 里 `python scripts/x.py` | **`ModuleNotFoundError`** | ✅ **显式报错，不会静默导入生产代码** |
| **任何非生产目录用生产 `.venv` 跑脚本/起服务** | **`E:\Thisyi\trade\src`（生产代码）** | 🔴 **禁止** |

### 4.2.1 Agent workspace 分配（**用户 2026-10-24 选定方案 A**）

| Bot | `work_dir`（需用户在 Bot 设置里改） | 理由 |
| :--- | :--- | :--- |
| **Coder** | **`E:\Thisyi\trade-dev`** | feature worktree 建在 `E:\Thisyi\trade-dev\wt-<需求>`（用户 2026-10-24 定名）；沙箱只覆盖 trade-dev，**不含生产源码** |
| **Tester** | **`E:\Thisyi\trade-test`** | 只在 `test` 分支做独立验证 |
| **Chief / Ops** | `E:\Thisyi\trade`（现状） | 生产部署与巡检 |
| Product / Architect / Reviewer | `E:\Thisyi\trade`（现状，写 `docs/`） | 文档与评审 |

**git 操作分工**：**Coder 不做 git 写操作**（worktree 的元数据在主仓 `.git`，沙箱不覆盖也不应覆盖）——**commit / merge / tag 全部由 Chief 执行**。

> 🔴 **必须写死的一条**：配了 workspace 之后，**"不许改生产代码"的保障不是沙箱，而是**：
> ① 分支纪律（feature 从 `master` 切、禁止直推 `master`/`test`）；② Reviewer 签；③ 部署只从 `master`。
> **沙箱保证的是"测试与生产不混跑"**（venv / 数据 / 锁），**它不保证"没人改生产源码"** ——
> 今晚的越界改动就是证据：Coder 的 workspace 是生产目录，它就越界改在了生产目录。


**规则**：
1. **测试区只用 `trade-test\.venv`**，且该 venv **永远保持 `--no-install-project`**（一旦安装项目就会产生指向目录的 `.pth`，回到"跑错代码"的形态）；
2. **禁止从任何非生产目录调用 `E:\Thisyi\trade\.venv`**（脚本、`uv run`、起服务一律不行）；
3. **共享/测试 venv 上永不执行 `uv sync`**——它是 sync-to-lock，会**卸载 lock 里没有的依赖**，直接把 test 线打挂；
4. **测试区跑脚本必须显式 `PYTHONPATH=src`**（或经 pytest 执行）；**直接跑脚本报 `ModuleNotFoundError` 是预期行为，不是环境损坏**（写进纪律，防止 Tester 误判）；
5. feature 确需新依赖 → **该 feature 自带 venv**（各跑一次 `uv sync`），不影响 test 线。

**🔴 配套守卫（阻断级）—— 把"不共享"从纪律变成机制**：

在 `tests/conftest.py` 会话守卫里加**代码来源断言**：

```python
import futu_quant
from pathlib import Path

def _assert_code_origin() -> None:
    root = Path(__file__).resolve().parents[1]
    mod = Path(futu_quant.__file__).resolve()
    assert root in mod.parents, f"跑错代码：{mod} 不在 {root} 下 —— 验收无效"
```

| 场景 | `__file__` | 断言 |
| :--- | :--- | :--- |
| 正确配置（`--no-install-project` + `pythonpath=src`） | `<本worktree>/src/futu_quant/__init__.py` | ✅ 通过 |
| **误用生产 `.venv`** | **`E:\Thisyi\trade\src\...`** | 🔴 **失败，验收当场变红** |

**为什么必须有**：§4.2 的 5 条规则**都是纪律**（"不要用生产 venv"、"不要共享"）——
**而今晚已经证明纪律会被绕过**。**这条断言让"跑错代码"在验收阶段必然暴露**，
**不靠任何人记得**。**变异测试尤其依赖它**（否则"关掉过滤器 → 守卫转红"可能跑的是别处代码，**给出假绿**）。

### 4.3 测试区的两个已知缺口

1. **`data/` 是空的**（gitignored）—— 没有 DB、没有配置。
   跑 pytest 无影响（用 tmp DB），但**手动冒烟测试跑不起来**。
   **要求（Product 验收前置）**：**验收前必须 seed 一份最小数据集**（campaign / 候选合约 / 关键配置键），
   **seed 脚本随代码版本化**（`scripts/seed_test_env.py`），**并写明 seed 来源与内容**——否则「验收通过」可能只是「没跑起来」；
2. **锁文件天然隔离**（`data/*.lock` 为 CWD 相对）—— 测试区起服务**不会**与生产锁冲突 ✅

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
