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
    """回滚开关必须用 os.getenv 直读，不能反过来依赖 Settings（否则自举死锁）。"""
    source = inspect.getsource(config.settings_cache_disabled)
    assert "os.getenv" in source
    assert "Settings" not in source


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
