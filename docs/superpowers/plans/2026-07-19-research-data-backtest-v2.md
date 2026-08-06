# Research Data and Backtest V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional multi-source research data layer, auditable backtest V2, optional Qlib adapter, option evidence diagnostics, and a Chinese research UI without changing the Futu paper-trading execution boundary.

**Architecture:** Providers normalize external history into one schema and a router fills gaps into versioned Parquet partitions. Existing deterministic backtests gain portfolio metrics and chronological validation; Qlib consumes canonical research data but can only emit non-executable research signals. Option replay gains explicit evidence sufficiency.

**Tech Stack:** Python 3.11, pandas, pyarrow/Parquet, urllib, optional yfinance, optional pyqlib, Streamlit, pytest, Ruff.

## Global Constraints

- Futu `TrdEnv.SIMULATE` remains the only account, order, fill, and live execution boundary.
- Third-party feeds are research-only and never determine live order prices.
- Missing optional keys or packages must not prevent the app or services from starting.
- Internal timestamps are UTC; UI timestamps and copy are Chinese/Asia-Shanghai.
- Secrets are environment-only and must not be persisted or logged.
- No free full-OPRA claim and no fabricated mid-price fills.

---

### Task 1: Canonical market-data contracts and providers

**Files:**
- Create: `src/futu_quant/market_data.py`
- Create: `tests/test_market_data.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces `ProviderStatus`, `MarketDataError`, `MarketDataNotConfigured`, `normalize_bars()`, `FutuMarketDataProvider`, `AlpacaMarketDataProvider`, and `YahooMarketDataProvider`.
- `get_bars(symbol, start, end, interval="1d") -> pd.DataFrame` always returns the canonical columns from the design.

- [ ] **Step 1: Write failing provider contract tests**

```python
def test_normalize_bars_adds_provenance_and_utc():
    raw = pd.DataFrame({"time_key": ["2026-07-17"], "open": [1], "high": [2],
                        "low": [0.5], "close": [1.5], "volume": [10]})
    result = normalize_bars(raw, "US.AAPL", "FUTU", "FUTU", "QFQ")
    assert list(result.columns) == CANONICAL_BAR_COLUMNS
    assert str(result.loc[0, "timestamp"].tz) == "UTC"
    assert result.loc[0, "source"] == "FUTU"

def test_unconfigured_alpaca_is_nonfatal(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    assert not AlpacaMarketDataProvider.from_env().health().configured
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_market_data.py`
Expected: import failure because `futu_quant.market_data` does not exist.

- [ ] **Step 3: Implement provider contracts and optional adapters**

Implement immutable status records, strict canonical normalization, Futu broker injection, Alpaca HTTPS with timeout and redacted errors, and lazy `yfinance` import. Add optional groups:

```toml
marketdata = ["yfinance>=0.2.65"]
research = ["pyqlib>=0.9.7"]
```

- [ ] **Step 4: Verify GREEN**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_market_data.py`
Expected: all tests pass without keys, yfinance, or pyqlib installed.

### Task 2: Canonical Parquet store, source routing, and quality reconciliation

**Files:**
- Create: `src/futu_quant/market_data_store.py`
- Create: `tests/test_market_data_store.py`
- Modify: `src/futu_quant/parquet_store.py`

**Interfaces:**
- Consumes canonical frames from Task 1.
- Produces `CanonicalMarketDataStore.write_bars()`, `read_bars()`, `catalog()`, `MarketDataRouter.get_bars()`, and `reconcile_sources()`.

- [ ] **Step 1: Write failing versioning and conflict tests**

```python
def test_store_deduplicates_and_records_manifest(tmp_path):
    store = CanonicalMarketDataStore(tmp_path)
    manifest = store.write_bars(canonical_frame(), "FUTU")
    store.write_bars(canonical_frame(), "FUTU")
    assert len(store.read_bars("US.AAPL")) == 1
    assert manifest.dataset_version and manifest.content_hash

def test_reconcile_quarantines_price_conflict():
    result = reconcile_sources(primary_frame(100), secondary_frame(120), 0.03)
    assert result.quality_status == "QUARANTINED"
    assert "收盘价差异" in result.reason
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_market_data_store.py`
Expected: import failure for the new store.

- [ ] **Step 3: Implement partitioned store and fallback router**

Write `data/market-data/bars/symbol=<safe>/year=<YYYY>/part.parquet`, atomic JSON manifests, exact-key deduplication, same-adjustment enforcement, provider-attempt diagnostics, and no silent overwrite of quarantined rows.

- [ ] **Step 4: Verify GREEN**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_market_data_store.py tests/test_data_quality.py`
Expected: all tests pass.

### Task 3: Backtest V2 metrics and benchmark attribution

**Files:**
- Create: `src/futu_quant/backtest_metrics.py`
- Create: `tests/test_backtest_v2.py`
- Modify: `src/futu_quant/backtest.py`

**Interfaces:**
- Produces `calculate_backtest_metrics(equity, trades, initial_cash, benchmark=None, periods_per_year=252)`.
- Existing result fields remain present; new metrics are additive.

- [ ] **Step 1: Write failing hand-calculated metric tests**

```python
def test_metrics_include_risk_trade_and_benchmark_values():
    equity = pd.DataFrame({"date": pd.bdate_range("2026-01-01", periods=4),
                           "equity": [100, 110, 99, 120], "position": [0, 1, 1, 0]})
    trades = pd.DataFrame([{"side": "SELL", "pnl": 20, "notional": 100}])
    benchmark = pd.Series([100, 102, 101, 105])
    metrics = calculate_backtest_metrics(equity, trades, 100, benchmark)
    assert metrics["total_return"] == pytest.approx(0.20)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)
    assert {"cagr", "sharpe", "sortino", "calmar", "profit_factor",
            "turnover", "beta", "alpha", "excess_return"} <= metrics.keys()
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_backtest_v2.py`
Expected: import failure for `backtest_metrics`.

- [ ] **Step 3: Implement metrics and integrate existing backtests**

Use daily percentage returns, a zero default risk-free rate, safe zero-variance handling, peak-to-recovery drawdown duration, trade notional turnover, and aligned benchmark returns. Preserve old keys and types.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_backtest_v2.py tests/test_backtest.py`
Expected: all tests pass.

### Task 4: Chronological walk-forward, embargo, and stress validation

**Files:**
- Modify: `src/futu_quant/walk_forward.py`
- Create: `tests/test_walk_forward_v2.py`

**Interfaces:**
- Produces `chronological_splits(size, train_size, validation_size, test_size, embargo)` and additive evidence fields from `walk_forward_validate()`.

- [ ] **Step 1: Write failing boundary tests**

```python
def test_splits_are_ordered_and_embargoed():
    split = chronological_splits(500, 250, 60, 60, 5)[0]
    assert split.train.stop + 5 <= split.validation.start
    assert split.validation.stop + 5 <= split.test.start

def test_stress_uses_one_one_point_five_and_two_times_slippage():
    result = walk_forward_validate(bars(400), "SMA Cross", 10000, 10, 1, 2)
    assert [row["滑点倍数"] for row in result["sensitivity"]] == [1.0, 1.5, 2.0]
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_walk_forward_v2.py`
Expected: missing function and old stress factors.

- [ ] **Step 3: Implement split evidence and deterministic bootstrap**

Reject overlapping windows, record train/validation/test dates and embargo, preserve seed 42, and include data version and benchmark metrics when supplied.

- [ ] **Step 4: Verify GREEN**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_walk_forward_v2.py tests/test_portfolio_walkforward_calibration.py`
Expected: all tests pass.

### Task 5: Optional Qlib research adapter

**Files:**
- Create: `src/futu_quant/qlib_adapter.py`
- Create: `tests/test_qlib_adapter.py`
- Modify: `src/futu_quant/experiments.py`

**Interfaces:**
- Produces `QlibStatus`, `QlibResearchAdapter.status()`, `prepare_dataset()`, and `normalize_predictions()`.

- [ ] **Step 1: Write failing optionality and signal tests**

```python
def test_missing_qlib_is_reported_not_raised(monkeypatch):
    adapter = QlibResearchAdapter(importer=lambda _: (_ for _ in ()).throw(ImportError()))
    assert not adapter.status().available

def test_predictions_become_non_executable_research_signals():
    rows = QlibResearchAdapter().normalize_predictions(predictions(), "m1", "data-v1")
    assert set(rows.columns) >= {"symbol", "score", "generated_at", "model_version",
                                "dataset_version", "executable"}
    assert not rows["executable"].any()
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_qlib_adapter.py`
Expected: import failure for `qlib_adapter`.

- [ ] **Step 3: Implement lazy adapter and experiment artifact metadata**

Do not import qlib at module import time. Validate canonical data, export symbol/date indexed research frames, normalize predictions with `executable=False`, and include adapter/data versions in experiment metadata.

- [ ] **Step 4: Verify GREEN**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_qlib_adapter.py tests/test_strategy_plugins_experiments_attribution.py`
Expected: all tests pass without pyqlib installed.

### Task 6: Option evidence sufficiency and research buckets

**Files:**
- Create: `src/futu_quant/option_evidence.py`
- Create: `tests/test_option_evidence.py`
- Modify: `src/futu_quant/option_event_backtest.py`
- Modify: `pages/13_options_data_backtest.py`

**Interfaces:**
- Produces `OptionEvidence`, `assess_option_evidence(frame, min_samples=5, min_days=1)`, and `option_research_buckets(frame)`.

- [ ] **Step 1: Write failing evidence tests**

```python
def test_insufficient_evidence_explains_gap():
    evidence = assess_option_evidence(two_good_rows(), min_samples=5, min_days=2)
    assert evidence.status == "INSUFFICIENT_EVIDENCE"
    assert evidence.missing_samples == 3
    assert "还缺 3 条" in evidence.reason

def test_unusable_books_never_count_as_fill_evidence():
    evidence = assess_option_evidence(wide_or_zero_bid_rows(), min_samples=1)
    assert evidence.usable_samples == 0
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_option_evidence.py`
Expected: import failure for `option_evidence`.

- [ ] **Step 3: Implement evidence diagnostics and guard replay**

Count unique New York trading dates, usable bid/ask rows, stale/degraded ratios, average spread, DTE/Delta/type/liquidity buckets, and raise a Chinese evidence error before replay when insufficient.

- [ ] **Step 4: Verify GREEN**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_option_evidence.py tests/test_option_history_backtest.py`
Expected: all tests pass.

### Task 7: Chinese data research center and navigation

**Files:**
- Create: `pages/22_data_research_center.py`
- Modify: `app.py`
- Create: `tests/test_research_data_ui.py`
- Modify: `tests/test_streamlit_navigation.py`

**Interfaces:**
- Consumes provider statuses, canonical catalog, data-quality diagnostics, backtest V2 metrics, Qlib status, and option evidence.

- [ ] **Step 1: Write failing static UI tests**

```python
def test_navigation_contains_data_research_center():
    source = Path("app.py").read_text()
    assert "pages/22_data_research_center.py" in source

def test_research_page_has_lazy_actions_and_chinese_sections():
    source = Path("pages/22_data_research_center.py").read_text()
    assert "数据源状态" in source and "数据目录" in source and "数据质量" in source
    assert "st.form" in source
```

- [ ] **Step 2: Verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_research_data_ui.py`
Expected: missing page/navigation assertions fail.

- [ ] **Step 3: Implement the page with bounded caches and explicit fetch forms**

Use native bordered containers, Chinese column configuration, no custom CSS, no `use_container_width`, no unguarded external calls in tabs, and add the page under “研究与系统”.

- [ ] **Step 4: Verify GREEN and page execution**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_research_data_ui.py tests/test_streamlit_navigation.py`
Then: `UV_CACHE_DIR=.uv-cache uv run python scripts/check_streamlit_pages.py`
Expected: tests and page compilation pass.

### Task 8: Full regression, safety, and operator documentation

**Files:**
- Modify: `README.md`
- Create: `docs/研究数据与回测V2使用说明.md`

**Interfaces:**
- Documents optional environment variables, dependency extras, data provenance, backfill command/UI, and Qlib limitations.

- [ ] **Step 1: Add a safety regression assertion if absent**

```python
def test_broker_is_always_simulate(settings):
    assert FutuPaperBroker(settings).trd_env == TrdEnv.SIMULATE
```

- [ ] **Step 2: Run the focused safety test before documentation changes**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_safety_and_strategies.py`
Expected: paper-only safety passes.

- [ ] **Step 3: Document setup and source boundaries**

Document `uv sync --extra marketdata`, optional `uv sync --extra research`, Alpaca variables, fallback behavior, data locations, Chinese UI flow, and the rule that research feeds never set live prices.

- [ ] **Step 4: Run complete verification**

Run: `UV_CACHE_DIR=.uv-cache uv run ruff check .`
Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q`
Run: `UV_CACHE_DIR=.uv-cache uv run python scripts/check_streamlit_pages.py --runtime`
Run: `lsof -nP -iTCP:8501 -sTCP:LISTEN`
Expected: Ruff exits 0, all tests pass, page runtime check passes, and the existing Streamlit listener remains available.

