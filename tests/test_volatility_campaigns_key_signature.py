"""#1 第 1 步回归：`tenant_id` 形参的引入必须是**零行为改动**。

判据（Chief 10-23 裁定，逐条可证）：

> `tenant_id=None` 时，读到的键、写回的键、返回的内容，必须与改动前**逐个等价**。

具体化为三条，全部用"记录调用参数"的方式验证（不碰生产库、不写 `.env`）：

1. **读**：`load_volatility_campaigns()` 必须读**无前缀旧键** ``VOLATILITY_CAMPAIGNS``；
2. **写**：`save_volatility_campaigns(payload)` 必须把 DB 与 `.env` 镜像都落在**同一个旧键**上，
   且**不产生**任何 ``VOLATILITY_CAMPAIGNS:*`` 租户键；
3. **内容**：读返回的就是存进去的内容（往返等价），序列化形态不变。

以及反向的两条：传租户时必须走租户键（否则"零行为改动"是假的，而租户化也没生效）。

**为什么不与"改动前的代码"对比**：那需要同时装载两个版本的模块，等于给测试开一条
"两份实现并存"的路。这里守的是**契约本身**（键与内容），它比"和旧代码长得一样"更强：
只要键错一个字符，这里就红。
"""
from __future__ import annotations

import json

import pytest

import futu_quant.config as config
from futu_quant.config import load_volatility_campaigns, save_volatility_campaigns

LEGACY_KEY = "VOLATILITY_CAMPAIGNS"
REAL = "futu_real_master"
PAYLOAD = [
    {"code": "US.META261030P660000", "tenant_id": REAL, "enabled": True},
    {"code": "US.QQQ261029C735000", "tenant_id": "", "enabled": True},
]


class _Recorder:
    """记录被调用的键与值，不触达任何真实存储。"""

    def __init__(self, return_value=None):
        self.keys: list[str] = []
        self.values: list[object] = []
        self.return_value = return_value

    def __call__(self, key, *args, **kwargs):
        self.keys.append(key)
        self.values.append(args[0] if args else kwargs.get("value"))
        return self.return_value


@pytest.fixture
def recorders(monkeypatch):
    """把读、写、镜像三条出口全部换成记录器（生产库与 .env 都不碰）。"""
    load = _Recorder(return_value=PAYLOAD)
    mirror = _Recorder()
    monkeypatch.setattr(config, "_load_runtime_value", load)
    monkeypatch.setattr(config, "_mirror_env", mirror)

    class _Store:
        def __init__(self, *args, **kwargs):
            self.writes: list[tuple[str, object, str]] = []

        def set_runtime_config(self, key, value, actor="system"):
            self.writes.append((key, value, actor))
            return 1

    store_instance = _Store()
    monkeypatch.setattr("futu_quant.store.TradingStore", lambda *a, **k: store_instance)
    monkeypatch.setattr(config, "ENV_FILE", config.DEFAULT_ENV_FILE)  # 走 DB 分支

    return load, mirror, store_instance


def test_read_without_tenant_uses_legacy_key(recorders):
    load, _mirror, _store = recorders

    result = load_volatility_campaigns()

    assert load.keys == [LEGACY_KEY], (
        f"无租户读取没有落在无前缀旧键上（实际 {load.keys}）—— "
        "这与签名引入前的行为不等价"
    )
    assert result == PAYLOAD, "读返回的内容与存储内容不等价"


def test_write_without_tenant_uses_legacy_key_only(recorders):
    load, mirror, store = recorders

    save_volatility_campaigns(PAYLOAD)

    assert store.writes == [(LEGACY_KEY, PAYLOAD, "streamlit_or_service")], (
        f"DB 写入的键不对（实际 {[w[0] for w in store.writes]}）—— "
        "写到了别的键就是「写进去没人读」的半状态"
    )
    # 镜像必须与 DB 用同一个键：两处键不一致 = 读侧和写侧各说各话
    assert mirror.keys == [LEGACY_KEY], f".env 镜像的键不对（实际 {mirror.keys}）"
    # 绝不能产生租户键 —— 第 1 步不许切流
    written_keys = [write[0] for write in store.writes]
    assert written_keys + mirror.keys == [LEGACY_KEY, LEGACY_KEY], (
        f"第 1 步就写出了别的键（实际 {written_keys + mirror.keys}）—— "
        "切流必须在第 3/4 步显式进行"
    )


def test_content_round_trip_is_unchanged(recorders):
    _load, mirror, _store = recorders

    save_volatility_campaigns(PAYLOAD)

    mirrored = mirror.values[0]
    assert isinstance(mirrored, str), "镜像值必须是 JSON 字符串（与改动前一致）"
    assert json.loads(mirrored) == PAYLOAD, "镜像内容与写入内容不等价"


def test_store_campaigns_without_tenant_uses_legacy_key(monkeypatch):
    """`store.campaigns()` 默认路径必须仍然读旧键，且不因新形参改变。"""
    seen: list[str | None] = []

    def fake_load(tenant_id=None):
        seen.append(tenant_id)
        return PAYLOAD

    monkeypatch.setattr("futu_quant.config.load_volatility_campaigns", fake_load)

    # 直接调用未绑定方法：不必为测试构造真实 TradingStore（它需要真实 DB）
    from futu_quant.store import TradingStore

    result = TradingStore.campaigns(object.__new__(TradingStore))
    assert seen == [None], f"默认路径传给 load_volatility_campaigns 的租户不是 None（实际 {seen}）"
    assert result == PAYLOAD


def test_store_campaigns_still_rejects_unknown_config_key(monkeypatch):
    """误传别的键必须继续响亮失败 —— 新形参不得把这道校验洗掉。"""
    from futu_quant.store import TradingStore

    with pytest.raises(ValueError):
        TradingStore.campaigns(object.__new__(TradingStore), config_key="SOME_OTHER_KEY")


def test_tenant_path_uses_tenant_key(recorders):
    """反向：传租户必须走租户键，且与旧键**互不干扰**（否则租户化没生效）。"""
    load, mirror, store = recorders

    save_volatility_campaigns(PAYLOAD, tenant_id=REAL)

    expected = f"{LEGACY_KEY}:{REAL}"
    assert store.writes == [(expected, PAYLOAD, "streamlit_or_service")], (
        f"租户写入没有落在 {expected} 上（实际 {[w[0] for w in store.writes]}）"
    )
    assert mirror.keys == [expected], f"租户镜像键不对（实际 {mirror.keys}）"

    load.keys.clear()
    load_volatility_campaigns(tenant_id=REAL)
    assert load.keys == [expected], f"租户读取没有落在 {expected} 上（实际 {load.keys}）"


def test_tenant_and_legacy_are_separate_namespaces(recorders):
    """两个命名空间必须彼此独立：读旧键读不到租户内容，反之亦然。"""
    load, _mirror, _store = recorders

    # 旧键 → 记录器返回 PAYLOAD；租户键 → 返回另一份内容
    responses = {LEGACY_KEY: PAYLOAD, f"{LEGACY_KEY}:{REAL}": []}

    def fake_load(key, default=None):
        return responses.get(key, default)

    import futu_quant.config as cfg

    original = cfg._load_runtime_value
    cfg._load_runtime_value = fake_load  # type: ignore[assignment]
    try:
        assert load_volatility_campaigns() == PAYLOAD, "旧键读到的内容不对"
        assert load_volatility_campaigns(tenant_id=REAL) == [], "租户键读到了旧键的内容"
    finally:
        cfg._load_runtime_value = original  # type: ignore[assignment]


def test_key_helper_is_pure_and_stable():
    """键函数本身：无租户 → 旧键；租户 → 租户键；空串租户视为无租户。"""
    from futu_quant.config import _volatility_campaigns_key

    assert _volatility_campaigns_key(None) == LEGACY_KEY
    assert _volatility_campaigns_key("") == LEGACY_KEY
    assert _volatility_campaigns_key(REAL) == f"{LEGACY_KEY}:{REAL}"
