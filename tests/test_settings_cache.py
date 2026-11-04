"""`Settings.load_cached()` 失效口径守卫（Reviewer 设计稿 2026-11-04）。

钉住三件事，缺一条就是「缓存了但永不失效」或「缓存进了下单/风控路径」：
1. UI 保存配置后，最坏时间窗内页面必须读到新值 —— 包括 .env 镜像写失败被吞的情形；
2. `Settings.load(refresh=True)` 不受缓存影响，Service 每轮仍拿最新 SQLite 值；
3. 命中缓存不会让 os.environ 与 Settings 双源漂移，多线程并发取值一致。
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
from pathlib import Path

import pytest

from futu_quant import config
from futu_quant.config import Settings
from futu_quant.store import TradingStore

KEY = "REAL_MAX_ORDER_NOTIONAL"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """隔离 .env + 独立 SQLite 库，并清空进程内缓存状态。"""
    env_file = tmp_path / ".env"
    env_file.write_text("FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    # save_runtime_settings 只在「ENV_FILE 就是默认 .env」时才写 SQLite，
    # 测试里把默认值一起指到 tmp，才能复现「写库成功 + 镜像写失败」。
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.setenv(KEY, "100")
    config.reset_settings_cache()
    yield env_file, TradingStore(tmp_path / "config.db")
    config.reset_settings_cache()


def test_save_is_visible_immediately_even_when_env_mirror_write_fails(isolated, monkeypatch):
    env_file, store = isolated
    assert Settings.load_cached(store=store).real_max_order_notional == 100

    def boom(*args, **kwargs):
        raise PermissionError("模拟 .env 被占用/只读")

    monkeypatch.setattr(config, "set_key", boom)
    config.save_runtime_settings({KEY: "321"}, store=store)

    # .env 内容没变（mtime 也没变），只有进程写入计数与 SQLite 版本变了：
    # 只靠 mtime 的实现会在这里永久读到 100。
    assert "321" not in env_file.read_text(encoding="utf-8")
    assert Settings.load_cached(store=store).real_max_order_notional == 321


def test_refresh_true_always_reads_latest_sqlite_value(isolated):
    _env_file, store = isolated
    assert Settings.load_cached(store=store, ttl_probe=60).real_max_order_notional == 100

    # 绕过 _mirror_env 直接改库 = 模拟「另一个进程写了权威配置」
    store.mutate_runtime_config(
        "ENV_SETTINGS", {}, lambda current: {**current, KEY: "777"}, "test",
    )

    # ttl_probe=60 期间不探测，缓存读仍是旧值；refresh=True 是硬 bypass，必须立刻读到 777。
    assert Settings.load_cached(store=store, ttl_probe=60).real_max_order_notional == 100
    assert Settings.load(refresh=True, store=store).real_max_order_notional == 777


def test_other_process_write_is_seen_within_probe_window(isolated):
    _env_file, store = isolated
    assert Settings.load_cached(store=store, ttl_probe=2.0).real_max_order_notional == 100
    store.mutate_runtime_config(
        "ENV_SETTINGS", {}, lambda current: {**current, KEY: "555"}, "test",
    )
    # ttl_probe=0 等价于「2 秒探测窗口到了」：下一次调用就能看见别人的写入。
    assert Settings.load_cached(store=store, ttl_probe=0.0).real_max_order_notional == 555


def test_two_writes_invalidate_even_when_mtime_is_frozen(isolated, monkeypatch):
    """Windows 上同秒两次写 mtime_ns 可能相同：失效不能只靠 mtime。"""
    _env_file, store = isolated
    monkeypatch.setattr(config, "_env_file_signature", lambda: ("frozen.env", 0, 0))
    monkeypatch.setattr(config, "set_key", lambda *a, **k: None)  # 镜像写成功但不改文件

    for value in ("200", "300"):
        config.save_runtime_settings({KEY: value}, store=store)
        assert Settings.load_cached(store=store).real_max_order_notional == int(value)


def test_env_file_switch_invalidates(isolated, monkeypatch, tmp_path):
    _env_file, store = isolated
    assert Settings.load_cached(store=store).ml_threshold != pytest.approx(0.99)

    other = tmp_path / "other.env"
    other.write_text(
        "FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\nML_THRESHOLD=0.99\n", encoding="utf-8"
    )
    monkeypatch.delenv("ML_THRESHOLD", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", other)
    assert Settings.load_cached(store=store).ml_threshold == pytest.approx(0.99)


def test_cache_hit_keeps_os_environ_and_settings_in_sync(isolated):
    _env_file, store = isolated
    first = Settings.load_cached(store=store)
    second = Settings.load_cached(store=store)
    assert second is first, "命中应当返回同一个 frozen 实例"
    assert float(os.environ[KEY]) == first.real_max_order_notional


def test_concurrent_readers_never_see_a_torn_settings(isolated):
    _env_file, store = isolated
    Settings.load_cached(store=store)
    seen: list[int] = []
    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(100):
                seen.append(id(Settings.load_cached(store=store)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    assert len(seen) == 2000
    assert len(set(seen)) == 1, "并发填充后应当只有一个共享实例"


def test_settings_cache_off_bypasses_cache(isolated, monkeypatch):
    _env_file, store = isolated
    monkeypatch.setenv("SETTINGS_CACHE", "off")
    assert Settings.load_cached(store=store) is not Settings.load_cached(store=store)


def test_backstop_marker_is_read_from_environment_not_settings():
    """回滚开关必须用 os.getenv 直读，不能反过来依赖 Settings（否则自举死锁）。

    只看函数体（AST），不看 docstring —— 注释里提到 Settings 不等于依赖 Settings。
    """
    tree = ast.parse(inspect.getsource(config.settings_cache_disabled))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # 跳过 docstring
    code = "\n".join(ast.dump(node) for node in body)
    assert "getenv" in code, "回滚开关没有用 os.getenv 直读环境变量"
    assert "'Settings'" not in code, "回滚开关不许依赖 Settings（会自举死锁）"


def test_load_itself_was_not_cached():
    """`load()` 本体不许被顺手缓存：service / 风控 / 实盘路径都还调它。"""
    assert "cache" not in inspect.getsource(Settings.load).lower()


def test_service_and_live_paths_keep_refresh_true():
    """静态守卫：Service 与实盘页的 `Settings.load(` 必须带 `refresh=True`。"""
    root = Path(__file__).resolve().parents[1]
    bare: list[str] = []
    for relative in ("scripts/run_service.py", "pages/27_live_trading_control.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "load"):
                continue
            if ast.unparse(func.value) != "Settings":
                continue
            refresh = next((kw for kw in node.keywords if kw.arg == "refresh"), None)
            if refresh is None or ast.unparse(refresh.value) != "True":
                bare.append(f"{relative}:{node.lineno}")
    assert not bare, f"这些 Settings.load( 没带 refresh=True：{bare}"


# ---------------------------------------------------------------- B 类页面的写入链路
# B 类页面（会写配置）依赖「保存完立刻读到新值」。它们最终都落到下面这些 writer，
# 所以按 writer 逐个钉，比按页面数钉更准：页面新增也只是复用同一批写入口。

@pytest.fixture
def env_only(tmp_path, monkeypatch):
    """只换 ENV_FILE（不是默认 .env）：writer 走镜像路径，不碰任何生产库。"""
    env_file = tmp_path / ".env"
    env_file.write_text("FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    config.reset_settings_cache()
    yield env_file
    config.reset_settings_cache()


def test_every_settings_writer_is_visible_immediately(env_only):
    cases = [
        ("save_trading_preferences", lambda: config.save_trading_preferences(("US.AAPL", "US.MSFT"), 3),
         "watchlist", ("US.AAPL", "US.MSFT")),
        ("save_gamma_symbols", lambda: config.save_gamma_symbols(("US.TSLA",)),
         "gamma_symbols", ("US.TSLA",)),
        ("save_core_satellite_symbols", lambda: config.save_core_satellite_symbols(("US.SPY",)),
         "core_satellite_symbols", ("US.SPY",)),
        ("save_core_satellite_profile", lambda: config.save_core_satellite_profile("aggressive"),
         "core_satellite_profile", "aggressive"),
        ("save_volatility_symbols", lambda: config.save_volatility_symbols(("US.NVDA",)),
         "volatility_symbols", ("US.NVDA",)),
        ("save_real_trading_settings", lambda: config.save_real_trading_settings(
            "SIMULATE", False, "", [], 640, 1280, True),
         "real_max_order_notional", 640.0),
    ]
    Settings.load_cached()  # 先预热，确保后面每条都是「命中缓存之后再写」
    for name, write, attribute, expected in cases:
        write()
        assert getattr(Settings.load_cached(), attribute) == expected, f"{name} 写完没立刻生效"


def test_option_contract_writer_is_visible_immediately(env_only):
    Settings.load_cached()
    config.save_option_contracts([{"code": "US.AAPL260116C200000"}])
    assert config.load_option_contracts() == [{"code": "US.AAPL260116C200000"}]


# ------------------------------------------------------------ 删键（不是改值）不留残留

def test_deleted_key_does_not_linger_after_full_removal(isolated):
    """DB 与 .env 镜像都删掉同一个键 → 必须回到代码默认值，不能沿用旧值。"""
    env_file, store = isolated
    config.save_runtime_settings({KEY: "321"}, store=store)
    assert Settings.load_cached(store=store).real_max_order_notional == 321

    store.mutate_runtime_config(
        "ENV_SETTINGS", {}, lambda current: {k: v for k, v in current.items() if k != KEY}, "test",
    )
    env_file.write_text("FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n", encoding="utf-8")

    assert Settings.load_cached(store=store, ttl_probe=0.0).real_max_order_notional == 500.0


def test_db_only_deletion_never_returns_fresher_than_uncached_path(isolated):
    """只删库里的键（.env 镜像还在）：缓存读不得比直通/refresh 更旧，两者必须一致。

    注：这种情况两条路径都还看得到 .env 镜像里的旧值 —— 这是 .env 作为兼容镜像的
    既有语义（与本次缓存无关），所以这里钉的是「缓存不引入分歧」，不是「立刻变默认」。
    """
    env_file, store = isolated
    config.save_runtime_settings({KEY: "321"}, store=store)
    assert Settings.load_cached(store=store).real_max_order_notional == 321

    store.mutate_runtime_config(
        "ENV_SETTINGS", {}, lambda current: {k: v for k, v in current.items() if k != KEY}, "test",
    )

    cached_value = Settings.load_cached(store=store, ttl_probe=0.0).real_max_order_notional
    uncached_value = Settings.load(refresh=True, store=store).real_max_order_notional
    assert cached_value == uncached_value
    assert env_file.read_text(encoding="utf-8").find(str(int(cached_value))) >= 0


# ------------------------------------------------------- B1：读库失败必须 miss 并重解析

def test_unreadable_db_forces_miss_instead_of_reusing_last_version(isolated, monkeypatch):
    """读库抛异常时不许沿用上一次的 version —— 否则签名不变、陈旧可能无限期。"""
    env_file, store = isolated
    assert Settings.load_cached(store=store).real_max_order_notional == 100

    def unreadable(key):
        raise RuntimeError("模拟配置库不可读")

    monkeypatch.setattr(store, "get_runtime_config", unreadable)
    monkeypatch.delenv(KEY, raising=False)
    env_file.write_text(
        f"FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n{KEY}=222\n", encoding="utf-8"
    )

    # 缓存里还是 100；读库失败必须强制重新解析，读到 .env 的新值 222
    assert Settings.load_cached(store=store, ttl_probe=0.0).real_max_order_notional == 222

    # 而且「库一直读不到」期间不许把答案固化：再改 .env 仍然要生效
    env_file.write_text(
        f"FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n{KEY}=333\n", encoding="utf-8"
    )
    assert Settings.load_cached(store=store, ttl_probe=0.0).real_max_order_notional == 333


def test_signature_separates_different_stores(tmp_path, monkeypatch):
    """两个不同库同 version 不许互相命中（B4 的 store 维度）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("FUTU_TRADING_ENV=SIMULATE\nFUTU_MARKET=US\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.setenv(KEY, "100")
    config.reset_settings_cache()

    store_a = TradingStore(tmp_path / "a.db")
    store_b = TradingStore(tmp_path / "b.db")
    store_a.mutate_runtime_config("ENV_SETTINGS", {}, lambda current: {**current, KEY: "111"}, "test")
    store_b.mutate_runtime_config("ENV_SETTINGS", {}, lambda current: {**current, KEY: "999"}, "test")

    assert Settings.load_cached(store=store_a, ttl_probe=0.0).real_max_order_notional == 111
    assert Settings.load_cached(store=store_b, ttl_probe=0.0).real_max_order_notional == 999
    config.reset_settings_cache()


def test_process_injected_variables_are_never_overridden_by_db(isolated, monkeypatch):
    """B3：TRADEYI_ENV / TRADEYI_PROD_DB_PATH / ENABLE_PAPER_ORDERS / SETTINGS_CACHE
    是 service_control.start() 注入的过程事实，DB 里的同名键不许盖掉它们。"""
    _env_file, store = isolated
    injected = {
        "TRADEYI_ENV": "test",
        "TRADEYI_PROD_DB_PATH": r"E:\Thisyi\trade\data\trading.db",
        "ENABLE_PAPER_ORDERS": "false",
    }
    for key, value in injected.items():  # 模拟 service_control.start() 的注入
        monkeypatch.setenv(key, value)
    poisoned = {
        "TRADEYI_ENV": "prod",
        "TRADEYI_PROD_DB_PATH": r"X:\poison.db",
        "ENABLE_PAPER_ORDERS": "true",
        "SETTINGS_CACHE": "off",
    }
    store.mutate_runtime_config(
        "ENV_SETTINGS", {}, lambda current: {**current, **poisoned}, "test",
    )
    config.reset_settings_cache()

    Settings.load_cached(store=store, ttl_probe=0.0)

    for key, value in injected.items():
        assert os.environ.get(key) == value, f"{key} 被数据库值覆盖了"
    assert config.settings_cache_disabled() is False, "SETTINGS_CACHE 不许被数据库值打开/关闭"


# 这些页面不吃缓存（Chief 2026-11-04 判据：能下单或能写配置的页面，一律不吃缓存）。
# 有人「顺手优化」回去就会用错账户或用错额度，所以用静态守卫钉死。
CACHING_FORBIDDEN_PAGES = (
    # 实盘/下单控制
    "pages/27_live_trading_control.py",
    "pages/4_order_management.py",
    "pages/9_launch_approval.py",
    "pages/16_options_combo_execution.py",
    "pages/5_earnings_gamma_strategy.py",
    # 会写配置 / 改运行状态
    "pages/1_trading_config.py",
    "pages/2_options_config.py",
    "pages/15_strategy_control_center.py",
    "pages/44_strategy_center.py",
    "pages/45_risk_and_limits.py",
)

# 纯展示页保留缓存（每页源码里都写了「为什么这页可以缓存」）。
CACHING_ALLOWED_PAGES = (
    "pages/00_today_overview.py",
    "pages/28_options_pnl_ledger.py",
    "pages/41_candidate_quality_center.py",
    "pages/42_ml_model_status.py",
)


def _calls_settings_load_cached(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "load_cached":
            if ast.unparse(func.value) == "Settings":
                lines.append(node.lineno)
    return lines


def test_order_and_account_pages_do_not_use_the_cache():
    root = Path(__file__).resolve().parents[1]
    for relative in CACHING_FORBIDDEN_PAGES:
        hits = _calls_settings_load_cached(root / relative)
        assert not hits, f"{relative} 第 {hits} 行吃了 Settings 缓存，下单/账户路径不许缓存"


def test_display_only_pages_keep_the_cache():
    """对称守卫：白名单里的展示页必须真的吃缓存，否则这条优化会悄悄退回。"""
    root = Path(__file__).resolve().parents[1]
    for relative in CACHING_ALLOWED_PAGES:
        hits = _calls_settings_load_cached(root / relative)
        assert hits, f"{relative} 是纯展示页，应当走 Settings.load_cached()"
