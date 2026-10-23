"""#1 键级分区（N 租户）—— 双租户隔离与剪枝保护的验收守卫。

口径（Chief 2026-10-23 定，Architect 设计 `docs/volatility_campaigns_tenant_partition_design.md`）：

* 键分区：``VOLATILITY_CAMPAIGNS:{tenant_id}`` —— 各租户各存一份清单；
* **写读必须原子切换**：写点开始写租户键、读点还读全局键 → 新写的 campaign
  **当场从读侧消失**。所以写点与读点必须同批改，不能拆；
* **剪枝不得删**：``order_push`` 写 ``volatility_long_gamma:{tenant}:{code}``，
  清理侧只认 ``volatility_long_gamma:{code}`` 时会把刚写进去的键当 stale 删掉
  （10-23 P0-9 实测，每一轮都在发生）。

本文件守三件事：
1. 两个租户各写一份、**互不覆盖**；
2. 键由 `tenant.campaigns_config_key` 拼，**不手拼**；
3. **写过之后剪枝不删**（D 批的验收判据）。
"""
from __future__ import annotations

import re
from pathlib import Path

from futu_quant import config, tenant
from futu_quant.store import TradingStore
from futu_quant.tenant import VOLATILITY_CAMPAIGNS_KEY

ROOT = Path(__file__).resolve().parents[1]
#: 扫描根 —— 变异测试可指向副本（证明"全部切流后守卫转绿"），生产扫描仍指向工作树。
SCAN_ROOT = ROOT
REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"

CAMPAIGN = {"code": "US.META261030P660000", "max_position_loss_ratio": 0.25,
            "max_position_loss_usd": 300.0}
OTHER_CAMPAIGN = {"code": "US.NVDA261106P215000", "max_position_loss_ratio": 0.30,
                  "max_position_loss_usd": 500.0}


# --------------------------------------------------------------------------- #
# 1. 双租户各写一份、互不覆盖
# --------------------------------------------------------------------------- #

def test_two_tenants_keep_independent_copies():
    """两个租户各写一份清单 → 各读各的，且**旧键不被污染**。

    这条是 #1 的核心验收：键分区若只做一半（写侧分区、读侧仍读全局键），
    两个租户会互相顶掉 —— 那正是 10-22 跨租户连坐的形态。
    """
    real_list = [{**CAMPAIGN, "tenant_id": REAL_TENANT}]
    paper_list = [{**OTHER_CAMPAIGN, "tenant_id": PAPER_TENANT}]

    config.save_volatility_campaigns(real_list, tenant_id=REAL_TENANT)
    config.save_volatility_campaigns(paper_list, tenant_id=PAPER_TENANT)

    assert config.load_volatility_campaigns(tenant_id=REAL_TENANT) == real_list, (
        "实盘租户读回的不是自己写的那份 —— 键分区未生效"
    )
    assert config.load_volatility_campaigns(tenant_id=PAPER_TENANT) == paper_list, (
        "模拟租户读回的不是自己写的那份 —— 两个租户互相覆盖了"
    )
    # 旧键保持它自己的内容（#1 第 1 步的零行为改动要求）
    legacy = config.load_volatility_campaigns()
    assert legacy != real_list and legacy != paper_list, (
        f"无前缀旧键被租户写入污染：{legacy}"
    )


def test_same_code_coexists_across_tenants():
    """同一个 `code` 在两个租户下**必须共存**，且各自带自己的 `tenant_id`。"""
    real_list = [{**CAMPAIGN, "tenant_id": REAL_TENANT}]
    paper_list = [{**CAMPAIGN, "tenant_id": PAPER_TENANT}]
    config.save_volatility_campaigns(real_list, tenant_id=REAL_TENANT)
    config.save_volatility_campaigns(paper_list, tenant_id=PAPER_TENANT)

    got_real = config.load_volatility_campaigns(tenant_id=REAL_TENANT)
    got_paper = config.load_volatility_campaigns(tenant_id=PAPER_TENANT)
    assert len(got_real) == 1 and len(got_paper) == 1, "同一个 code 没有共存"
    assert got_real[0]["code"] == got_paper[0]["code"] == CAMPAIGN["code"]
    assert got_real[0]["tenant_id"] == REAL_TENANT, "实盘那份的租户身份丢了"
    assert got_paper[0]["tenant_id"] == PAPER_TENANT, "模拟那份的租户身份丢了"


# --------------------------------------------------------------------------- #
# 2. 键由 tenant 层拼，不得手拼
# --------------------------------------------------------------------------- #

def test_tenant_key_is_built_by_the_tenant_layer():
    """`config` 层必须走 `tenant.campaigns_config_key`，两处不得各拼各的。"""
    for tenant_id in (REAL_TENANT, PAPER_TENANT):
        assert config._volatility_campaigns_key(tenant_id) == tenant.campaigns_config_key(tenant_id), (
            f"config 层与 tenant 层对 {tenant_id} 拼出的键不一致 —— 写读会分叉"
        )
        assert config._volatility_campaigns_key(tenant_id) == f"{VOLATILITY_CAMPAIGNS_KEY}:{tenant_id}"
    # 无租户 → 必须仍是无前缀旧键（存量调用点的零行为改动）
    assert config._volatility_campaigns_key(None) == VOLATILITY_CAMPAIGNS_KEY


def test_store_reads_the_same_tenant_key(tmp_path, monkeypatch):
    """`store.campaigns(tenant_id=...)` 必须把租户**原样传下去**，不得自己拼键。

    它内部委托 `config.load_volatility_campaigns`（后者再用 `tenant.campaigns_config_key`）。
    若哪天有人在这里手拼 `f"{key}:{tenant}"`，就会与写侧分叉 —— 写进去没人读。
    """
    seen: list[object] = []
    monkeypatch.setattr(
        config, "load_volatility_campaigns",
        lambda tenant_id=None: seen.append(tenant_id) or [],
    )
    store = TradingStore(tmp_path / "campaigns.db")
    store.campaigns(tenant_id=REAL_TENANT)
    assert seen == [REAL_TENANT], (
        f"store.campaigns 没有把租户传给配置层（实际收到 {seen}）—— "
        "它可能自己拼了键，写读会分叉"
    )


# --------------------------------------------------------------------------- #
# 3. 写过之后剪枝不删（D 批的验收判据）
# --------------------------------------------------------------------------- #

PREFIX = "volatility_long_gamma:"
CODE = "US.META261030P660000"
TENANT_STATE_KEY = f"{PREFIX}{REAL_TENANT}:{CODE}"


def test_tenant_namespaced_state_survives_pruning(tmp_path):
    """`order_push` 写的带租户状态键，在**只给 active_codes** 时必须保留。

    这是 D 批的验收：清理侧拿不到租户上下文时，只要合约代码命中在管清单就不删。
    若把这条改坏（退回只认 active_keys），刚写进去的键会被当 stale 当场删掉 ——
    10-23 P0-9 实测的形态。
    """
    store = TradingStore(tmp_path / "states.db")
    store.save_campaign_states({TENANT_STATE_KEY: {"fill_registered_at": "2026-10-23T00:00:00Z"}})

    removed = store.prune_campaign_states(PREFIX, active_keys=set(), active_codes={CODE})

    assert TENANT_STATE_KEY in store.campaign_states(), (
        f"带租户的状态键被剪枝删掉了（删除 {removed} 条）—— 写进去当场消失，"
        "「成交即登记」被完全抵消"
    )


def test_pruning_without_active_codes_would_delete_it(tmp_path):
    """反向对照：**不给** active_codes 时它确实会被删 —— 证明上一条不是恒真。"""
    store = TradingStore(tmp_path / "states.db")
    store.save_campaign_states({TENANT_STATE_KEY: {"fill_registered_at": "2026-10-23T00:00:00Z"}})

    store.prune_campaign_states(PREFIX, active_keys=set())

    assert TENANT_STATE_KEY not in store.campaign_states(), (
        "不带 active_codes 也没删 —— 说明剪枝判据与预期不同，上一条的绿是偶然的"
    )


def test_worker_prune_receives_active_codes_and_avoids_global_prefix_prune():
    """D 批验收 + 10-23 跨租户剪枝修复：worker 剪枝必须传 active_codes，且不得再用全局前缀剪枝。

    库层有保护（`active_codes` 参数）还不够 —— **调用点不传就等于没有**。
    更严重的是 10-23 开盘实测：全局前缀 `prune_campaign_states` 只按「本租户」
    判定 stale，会把另一租户的带租户状态键删掉（实盘 peak 每 2 分钟归零）。
    因此调用点必须走 `prunable_state_keys(store, tenant_id, active_keys, active_codes)`。
    """
    text = (SCAN_ROOT / "scripts/run_volatility_strategy.py").read_text(encoding="utf-8")
    assert "prune_campaign_states(" not in text, (
        "worker 又用回全局前缀剪枝 —— 会跨租户删掉别人的状态键（实盘 peak 归零）"
    )
    match = re.search(r"prunable_state_keys\(", text)
    assert match, "worker 不再调用 prunable_state_keys —— 清理路径变了，守卫需同步"
    call = text[match.start():match.start() + 400].split("\n\n")[0]
    assert "active_codes" in call and "active_keys" in call, (
        f"worker 的剪枝调用没同时传 active_keys / active_codes：\n{call}\n"
        "—— 带租户的状态键会被当 stale 删掉，「成交即登记」被抵消"
    )


def test_order_push_never_writes_a_tenantless_state_key():
    """D 批验收：`order_push` 不得再写**不带租户段**的状态键。

    写了带租户键、又在别处兜底写无前缀键，两种形态会**同时存活** ——
    worker 每轮把它当两个 campaign 执行（`_campaign_identity` docstring 自己警告过）。
    """
    text = (SCAN_ROOT / "src" / "futu_quant" / "order_push.py").read_text(encoding="utf-8")
    assert "volatility_long_gamma:{symbol}" not in text, (
        "order_push 里仍有不带租户段的状态键拼接（`volatility_long_gamma:{symbol}`）—— "
        "带租户与不带租户两种形态会并存，worker 每轮当两个 campaign 执行"
    )
    assert '"volatility_long_gamma:" + symbol' not in text, (
        "order_push 里仍有 `\"volatility_long_gamma:\" + symbol` 这种无前缀拼接"
    )


# --------------------------------------------------------------------------- #
# 4. 写点 / 读点必须切到租户键（#1 第 3/4 步的开关）
# --------------------------------------------------------------------------- #

WRITE_SIDE_FILES = (
    "src/futu_quant/volatility_scanner.py",
    "src/futu_quant/order_push.py",
    "src/futu_quant/volatility_orphans.py",
    "src/futu_quant/campaign_cleanup.py",
    "scripts/run_volatility_strategy.py",
    "pages/10_volatility_options_strategy.py",
    "pages/11_earnings_gamma_strategy.py",
)
READ_SIDE_FILES = (
    "src/futu_quant/volatility_scanner.py",
    "src/futu_quant/order_push.py",
    "src/futu_quant/volatility_orphans.py",
    "src/futu_quant/campaign_cleanup.py",
    "scripts/run_volatility_strategy.py",
    "pages/10_volatility_options_strategy.py",
    "pages/11_earnings_gamma_strategy.py",
)


def _call_sites(rel_path: str, func_name: str) -> list[tuple[int, str]]:
    """找出 `func_name(` 的调用点，并把多行调用的文本一起返回（含 `tenant_id=`）。"""
    path = SCAN_ROOT / rel_path
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    sites: list[tuple[int, str]] = []
    for match in re.finditer(rf"\b{func_name}\(", text):
        line_no = text[:match.start()].count("\n") + 1
        tail = text[match.start():match.start() + 400]
        sites.append((line_no, tail.split("\n\n")[0]))
    return sites


def _first_arg(snippet: str) -> str:
    """从 `func(a, b)` 的调用片段里取出第一个参数表达式。"""
    if "(" not in snippet:
        return ""
    body = snippet.split("(", 1)[1]
    depth = 0
    out: list[str] = []
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def _key_is_tenant_scoped(rel_path: str, var_name: str) -> bool:
    """**追数据来源，不追变量名**：这个变量是不是从租户键入口派生出来的。

    只认 `campaigns_config_key*()` 这一族入口，不认"变量名叫得像"。
    例：`_campaigns_key, _fell_back = campaigns_config_key_lenient(tenant_id)`
    """
    if not var_name.isidentifier():
        return False
    path = SCAN_ROOT / rel_path
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    pattern = r"^[ \t]*([A-Za-z_][A-Za-z0-9_,\s]*?)\s*=\s*(.+)$"
    for match in re.finditer(pattern, text, re.MULTILINE):
        names = [n.strip() for n in match.group(1).split(",")]
        if var_name in names and "campaigns_config_key" in match.group(2):
            return True
    return False


def test_write_points_use_a_tenant_scoped_key():
    """写点必须写**租户作用域的键**，不得写无前缀旧键。

    三种合法形态都算通过：
    * `save_volatility_campaigns(..., tenant_id=...)` —— 键由 config 层拼；
    * `upsert_campaign(campaigns_config_key(tenant), ...)` —— 键由 tenant 层拼；
    * `upsert_campaign(_campaigns_key, ...)`，而 `_campaigns_key` 由
      `campaigns_config_key_lenient(...)` 派生 —— **追赋值来源，不追变量名**。

    `upsert_campaign` 自己不接 `tenant_id`（它接的是**完整键**），所以判据是
    "这个键有没有从租户入口派生出来"，而不是"调用行有没有 `tenant_id` 字样"。
    """
    missing: list[str] = []
    for rel in WRITE_SIDE_FILES:
        for func in ("save_volatility_campaigns", "upsert_campaign"):
            for line_no, snippet in _call_sites(rel, func):
                scoped = (
                    "tenant_id" in snippet
                    or "campaigns_config_key" in snippet
                    or "tenant_of_broker" in snippet
                )
                if not scoped:
                    scoped = _key_is_tenant_scoped(rel, _first_arg(snippet))
                if not scoped:
                    missing.append(f"{rel}:{line_no}({func})")
    assert not missing, (
        f"这些写点仍在写无前缀旧键：{missing} —— "
        "写读未原子切换，会出现「写进去没人读」的半状态"
    )


def test_read_points_pass_tenant_id():
    """读点必须显式传 `tenant_id` —— 否则读的是旧键，新写的分区永远读不到。"""
    missing: list[str] = []
    for rel in READ_SIDE_FILES:
        for line_no, snippet in _call_sites(rel, "load_volatility_campaigns"):
            if "tenant_id" not in snippet:
                missing.append(f"{rel}:{line_no}")
    assert not missing, (
        f"这些读点仍在读无前缀旧键（缺 tenant_id=）：{missing} —— "
        "写侧已分区而读侧没有，campaign 会「当场从读侧消失」"
    )


# --------------------------------------------------------------------------- #
# 11. 状态键拼法必须一致（一个共享入口，一种形态）
# --------------------------------------------------------------------------- #

STATE_PREFIX = "volatility_long_gamma:"
#: 会构造或读取 `volatility_long_gamma:` 形态 campaign 状态键的文件
STATE_KEY_FILES = (
    "src/futu_quant/order_push.py",
    "src/futu_quant/volatility_orphans.py",
    "scripts/run_volatility_strategy.py",
    "scripts/run_service.py",
    "pages/10_volatility_options_strategy.py",
)
#: f-string 里出现"占位符含 tenant" = 带租户段（用正则而非字面量：
#: `{tenant}` / `{_tenant}` / `{_state_tenant}` / `{tenant_id}` 都算，
#: 判据写死字面量会把 `{_state_tenant}` 误判成无前缀）
TENANT_PLACEHOLDER = re.compile(r"\{[^{}]*tenant[^{}]*\}")


def test_all_volatility_state_keys_carry_a_tenant_segment():
    """所有 `volatility_long_gamma:` 状态键的拼法必须**一致**（带租户段）。

    17:3x 实测，四处三种格式：

    ================================  ==========================================
    `order_push.py`                   `volatility_long_gamma:{tenant}:{symbol}`  ✅
    `volatility_orphans.py`           `volatility_long_gamma:{item.code}`        ❌
    `pages/10_volatility_options_...` `volatility_long_gamma:{code}`              ❌
    `run_service.py`                  `volatility_long_gamma:{tenant}:{code}`    ✅
    ================================  ==========================================

    **后果**：迁移 A 把清单搬到租户键之后，`volatility_orphans` 写的状态读侧看不到，
    存量全局状态键里的 `peak` / `position_opened_at` / `entry_iv_actual`
    **止盈止损基准当场丢失** —— 违反「写侧必须是读侧的超集」。

    唯一允许的例外：**显式写明的兼容读**（同一语句里先读带租户形态、再回落旧形态），
    例如 ``states.get(tenant_form) or states.get(plain_form)``。
    那不是"两种拼法并存"，是"迁移期兼容"，且必须成对出现。
    """
    offenders: list[str] = []
    for rel in STATE_KEY_FILES:
        path = SCAN_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(r"""f(["'])""" + re.escape(STATE_PREFIX) + r"""[^"']*\1""")
        for match in pattern.finditer(text):
            literal = match.group(0)
            line_no = text[:match.start()].count("\n") + 1
            if "volatility_state_keys" in literal:
                continue  # 走共享助手，形态由它统一产出
            if TENANT_PLACEHOLDER.search(literal):
                continue
            window = text[max(0, match.start() - 160):match.end() + 120]
            compatible_read = (
                ".get(" in window
                and ("volatility_long_gamma:{tenant" in window or "{_tenant" in window)
            )
            if not compatible_read:
                offenders.append(f"{rel}:{line_no}: {literal}")
    assert not offenders, (
        "这些位置在拼**不带租户**的 volatility 状态键：\n  "
        + "\n  ".join(offenders)
        + "\n—— 与写侧（带租户）形态不一致，读侧会看不到；"
        "要么改成带租户形态，要么走共享助手 volatility_state_keys()"
    )
