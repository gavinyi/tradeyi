# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A US stock + options **paper-trading** quant system built on Futu OpenAPI (`futu-api`, talking to a local
**Futu OpenD** gateway). The UI and all operational docs are in **Chinese** — match that voice in any
user-facing strings, logs, and doc changes. Runtime is hard-locked to market `US` and (by default) trading
environment `SIMULATE`; real-money trading exists only as a heavily gated framework and must never become the
default.

Two runtime surfaces share one codebase:
- **Streamlit multipage app** (`app.py` + `pages/`) — configuration, monitoring, dashboards.
- **Unified scheduler service** (`scripts/run_service.py`) — the production runtime: trading-session logic,
  strategy scheduling, order reconciliation, repricing/cancellation, heartbeat, alerts, DR.

## Commands

Everything runs through **`uv`** (the lockfile is `uv.lock`; Python 3.11–3.13).

```bash
uv sync --extra dev                          # install + dev tools (pytest, ruff)
uv run python scripts/check_connection.py    # smoke-test OpenD; expect OK + SIMULATE
uv run streamlit run app.py                  # UI at http://localhost:8501
uv run python scripts/run_service.py         # unified trading service (single instance)
uv run python scripts/run_service.py --once  # one scheduling cycle, then exit
uv run python scripts/run_strategies.py --strategy "Core Satellite"   # run a strategy once
uv run python scripts/run_strategies.py --strategy "SMA Cross" --loop # loop (basic strategies)
uv run python scripts/run_gamma_strategy.py --loop --interval 30      # earnings gamma executor
uv run python scripts/run_volatility_strategy.py --loop               # volatility long gamma executor
uv run python scripts/set_admin_password.py  # set the PBKDF2 admin password (required for page controls)
uv run python scripts/check_streamlit_pages.py            # static lint of all Streamlit pages
uv run python scripts/check_streamlit_pages.py --runtime  # real render of each page (needs OpenD)
uv run python scripts/migrate_to_postgres.py --dry-run    # SQLite→Postgres is explicit/offline only
```

Testing & linting:

```bash
uv run pytest                       # run all tests
uv run pytest tests/test_real_trading.py            # single file
uv run pytest tests/test_real_trading.py::test_real_session_requires_correct_confirmation_code  # single test
uv run ruff check .                 # lint (line-length 100; excludes .venv, .uv-cache)
```

Optional dependency extras: `marketdata` (Yahoo fallback), `research` (Qlib adapter), `postgres`. Alpaca keys
go in env (`ALPACA_API_KEY` / secret) — never log the secret. **Futu is always the only authoritative source
for real-time pricing and live ordering**; Alpaca/Yahoo/Qlib are research-only backfill and never enter the
order-pricing path.

## Configuration model (read this before touching config)

This is the single most error-prone area. Configuration lives in **two places** and they are not equal:

- **SQLite `runtime_config` table (`data/trading.db`) is the runtime authority.** Settings changed from the
  UI are written here.
- **`.env` is a compatibility mirror.** Writes also mirror to `.env` via `config._mirror_env`, but `.env`
  is not guaranteed to reflect current runtime state.

`Settings.load()` (`src/futu_quant/config.py`) is the single source of truth for parsing + validating env
into a frozen `Settings` dataclass. It calls `refresh_runtime_environment()` to fold SQLite dynamic settings
back into `os.environ`. When adding/validating a knob: add the field + bounds check to `Settings.load()`,
default it in `.env.example`, and prefer the `save_*` helpers (which write to SQLite + mirror) over hand-editing
`.env`. `config.py` also defines symbol normalization (`normalize_us_symbol` — only `US.*` is accepted).

Test isolation relies on this split: tests set `config.ENV_FILE` to a temp path or `monkeypatch.setenv` +
`Settings.load()`, and point `TradingStore(tmp_path / "x.db")` at a throwaway DB.

## Architecture

### Layered layout

- `src/futu_quant/` — the library. Import root is `futu_quant` (pytest `pythonpath=["src"]`, setuptools
  `packages.find` under `src/`). ~90 modules; the load-bearing ones are listed below.
- `app.py` + `pages/` — Streamlit UI. `app.py` builds the sidebar explicitly via `st.navigation` (the
  `NN_名称.py` numbers only order files on disk; actual menu grouping is defined in `app.py`). Each page is a
  thin view over the library; do not put business logic in pages.
- `scripts/` — process entry points (service, strategy runners, ops/migration). `run_service.py` imports
  the `run_once` functions from the strategy scripts, so those scripts must stay importable as modules.
- `data/` — runtime state: `trading.db`, `option-chain-cache.db`, logs, single-instance `.lock`/`.pid`/
  `.stop` flag files, and partitioned Parquet market data. **All gitignored.** Treat as mutable runtime data.

### The unified execution path (every order flows through this)

`ExecutionManager.submit_limit()` (`execution.py`) is the only sanctioned path from "strategy decision" to
"broker order". The gate order matters:

1. **Execution-mode resolution**: per-strategy override `strategy_execution_mode:<id>` else global
   `EXECUTION_MODE`. One of `SHADOW` / `PAPER` / `REAL`.
2. **Approval gate**: if mode ∈ {PAPER, REAL} and `side == BUY` and approval is required and the strategy's
   current code+param+execution+risk fingerprint isn't approved → blocked. (Approval expiry never blocks
   SELL, so risk positions can't be locked in.)
3. **Idempotency**: `create_intent` keyed by `UNIQUE(strategy_id, intent_key)` — the intent_key carries the
   mode suffix (`:SHADOW`/`:PAPER`/`:REAL`), so the same logical order is submitted at most once per mode.
4. **Real-trade gate** (only if mode == REAL): `real_trading.check_real_trade()`.
5. **Risk gate**: `RiskEngine.check()` (`risk.py`) — notional/weight/Delta/Gamma/Vega/Theta limits.
6. **Submit**: SHADOW records intent + virtual match and **never calls the broker**; PAPER/REAL call
   `broker.place_limit_order` with `TrdEnv.SIMULATE` / `TrdEnv.REAL`.

`ExecutionManager` also owns reconciliation, smart initial pricing (`_initial_smart_price`), stale-order
cancellation (`ORDER_TIMEOUT_SECONDS`), and bounded repricing (`EXEC_*`). Combo legs are repriced at the
package level (`combo_execution.py`), never per-leg.

### Real-trading safety gates (`real_trading.py`)

A REAL order requires **all** of: `FUTU_TRADING_ENV=REAL` + `REAL_TRADING_ENABLED=true` + `REAL_ACCOUNT_ID` +
strategy in `REAL_ALLOWED_STRATEGIES` + per-order notional ≤ `REAL_MAX_ORDER_NOTIONAL` + running daily total ≤
`REAL_MAX_DAILY_NOTIONAL` + an active short-lived manual confirmation session. The confirmation code is
`REAL-YYYYMMDD-<last4 of account id>` and the session expires (default 30 min). **The app never stores a Futu
trade password** — real-account trading must be unlocked in OpenD/the Futu client. When adding behavior near
orders, preserve every gate; do not add new paths that bypass `submit_limit` / `check_real_trade`.

### Strategy model

Strategies are managed as **instances**, not per-page/per-symbol mixins. Each owns its own symbol pool,
params, execution mode, capital quota, approval evidence, and virtual-position ledger; option strategies
share execution/risk machinery but **not** exit logic (see `docs/strategy_architecture.md`).

- Stock: `core_satellite` (SPY-trend-gated core/satellite rebalancer, independent symbol list, runs in the
  pre-close window) and the basic `STRATEGIES` registry in `strategies.py` (`SMA Cross`, `RSI Reversion`,
  `20-day Breakout`) — pure decision functions `(DataFrame) -> StrategyResult`.
- Options: `earnings_gamma` (dual-source earnings event trigger), `volatility_long_gamma` (vol/trend/IV-structure
  trigger), and `earnings_gamma_manual` (one-shot page-submitted option order, no auto-management).

### State & reconciliation

`TradingStore` (`store.py`, SQLite + WAL) holds order intents, state events, decisions, audit trail,
runtime config, equity snapshots, heartbeats, alerts, etc. The schema is created idempotently in
`initialize()`. The service reconciles **three ledgers** each cycle: Futu broker positions ↔ local fill
ledger ↔ per-strategy virtual positions (`reconciliation.reconcile_ledgers`); a mismatch locks SELL on that
symbol, pauses the owning strategy, and raises an alert. Live order/fill **push** (`order_push.py`) updates
the ledger in real time; periodic order queries are only a polling fallback.

### Scheduler (`run_service.py`)

`service_cycle()` runs one dispatch pass; the main loop sleeps `--poll` seconds (default 10) between passes.
Tasks are gated by `USMarketCalendar` (XNYS via `exchange_calendars` — handles holidays/early-close/DST) and
`due()` interval timers, wrapped in `run_task()` which enforces per-task soft timeouts (`TASK_TIMEOUTS`),
exponential backoff on failure, and heartbeat/status writes. **Single-instance enforced** via
`fcntl.flock` on `data/trading-service.lock`. Do not run strategy `--loop` processes alongside the service.

## Conventions to preserve

- **Only `US.*` symbols**; route all user input through `normalize_us_symbol`.
- **No Futu trade secrets in the repo or DB**; secrets (webhook secret, admin hash, Alpaca key) are env-only.
- **Display time is China time** (`ui.china_time_columns` etc.); strategy/session logic internally uses
  America/New_York for exchange clocks — don't mix them.
- **Idempotency is the `(strategy_id, intent_key)` constraint** — reuse it, don't build parallel dedup.
- **Validation is defensive and explicit**: `Settings.load()` raises `RuntimeError` with a Chinese message on
  any bad value; mirror that style for new config knobs.
- Architecture/design docs live under `docs/` (Chinese) — update the relevant one when behavior changes.

## Branch & environment flow (user-mandated, 2026-10-29)

三个目录是**同一仓库的三个 worktree**，不是三个仓库。代码只能按 `dev → test → master` 单向流动：

| 区 | 路径 | 分支约定 | 角色 |
|---|---|---|---|
| 生产 | `E:\Thisyi\trade` | `master`（只合入，不直接改文件） | 生产 Worker + 面板（8501）从这里跑 |
| 开发 | `E:\Thisyi\trade-dev\wt-<slug>` | `feat/<slug>` 或 `fix/<slug>`，每个分支一个 worktree | 所有编码、提交发生在这里 |
| 测试 | `E:\Thisyi\trade-test` | 基于候选提交拉 `verify/<slug>` | 独立 `data/`、端口 8601、`TRADEYI_ENV=test` |

- **禁止在 `E:\Thisyi\trade` 里开发**：生产主工作树必须常驻 master 且保持干净，否则 `run_service` 的生产守卫会拒绝启动。
- 合入 master 前必须在 `trade-test` 跑全量回归并通过：`TRADEYI_ENV=test` + `TRADEYI_PROD_DB_PATH=E:\Thisyi\trade\data\trading.db` + `python -m pytest -q`（带生产库路径才会做指纹比对，不带就是拿测试库和自己比，等于没验）。
- 服务停摆等紧急情况可走 hotfix 直接合 master，但必须**当场说明是 hotfix**，事后补 trade-test 验证或回滚重走。
- 新建开发工作树的固定姿势：`git worktree add E:\Thisyi\trade-dev\wt-<slug> -b fix/<slug> master`。
- 以上三步用 `scripts/dev_worktree.py` 串起来：`new <slug>` 建树 → `verify --ref <branch>` 在测试区跑全量并写绿戳 → `merge <branch>` 校验绿戳后 ff 合入并推送；`release` 等于 verify+merge。**没有对应 commit 的绿戳时 `merge` 直接拒绝**，`--force` 仅用于 hotfix 并在提交说明里写清原因。
- 详见 `docs/test_environment_runtime.md`（测试区启停与验收清单）。
