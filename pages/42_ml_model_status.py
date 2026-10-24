"""ML 模型状态与打分开关（P2-1a 只读观测 + P2-3 租户级开关）。

回答 PRD 区块① 的问题，且**不提供任何写操作**（重训/配置属 1b 之后）：

① 当前生产模型是什么、**真实训练年龄**多少（来源必须是 pkl，绝不是元数据 json）；
② 现在到底在用 **ML 打分** 还是 **规则降级**；降级原因属于 PRD 五种里的哪一种；
③ 新鲜度指示与 §四 状态表同一套阈值（<30 绿 / 30–34 橙 / ≥35 红）；
④ `sklearn` 是否可用 —— 这决定了"装依赖后重启会切换打分方式"这件事什么时候发生。

薄视图：所有判定走 `futu_quant.ml_freshness`；开关读写走 `futu_quant.ml_config`，
页面不读文件、不评估阈值、不拼配置键。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ml_freshness import (
    FRESHNESS_LABELS,
    FRESHNESS_OK,
    FRESHNESS_STALE,
    FRESHNESS_WARN,
    SCORING_DISABLED,
    SCORING_LABELS,
    SCORING_ML,
    SCORING_RULE_FALLBACK,
    freshness_summary_line,
    ml_model_health,
)
from futu_quant.ml_config import (
    FACTOR_SPECS,
    MODE_LABELS,
    MODE_ML,
    MODE_RULE,
    resolve_ml_factor,
    resolve_scoring_mode,
    set_ml_factor,
    set_scoring_mode,
)
from futu_quant.store import TradingStore
from futu_quant.ui import get_current_tenant, render_admin_login, render_sidebar

st.set_page_config(page_title="ML 模型状态", page_icon="🧠", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("ML 模型状态（只读）")
st.caption(
    "这一页只回答「现在用的是 ML 还是规则、模型有多旧、为什么降级」。"
    "重训在后续步骤提供；本页唯一可写的是**当前租户**的 ML 打分开关。"
)

# 生效租户：ML 是否启用按租户配置解析；拿不到就诚实显示"未识别"，不猜。
try:
    page_tenant_id = str(get_current_tenant(settings).get("tenant_id") or "")
except Exception:
    page_tenant_id = ""

health = ml_model_health()

freshness = str(health.get("freshness") or "")
_freshness_tone = {
    FRESHNESS_OK: "🟢",
    FRESHNESS_WARN: "🟠",
    FRESHNESS_STALE: "🔴",
}.get(freshness, "⚪")
age = health.get("age_days")
age_text = f"{age:.1f} 天" if isinstance(age, (int, float)) else "未知"

scoring_mode = str(health.get("scoring_mode") or "")
with st.container(horizontal=True, border=True):
    st.metric("新鲜度", f"{_freshness_tone} {FRESHNESS_LABELS.get(freshness, '未知')}", border=True)
    st.metric("模型年龄", age_text, border=True)
    st.metric("打分方式", SCORING_LABELS.get(scoring_mode, scoring_mode or "未知"), border=True)
    st.metric("sklearn", "可用" if health.get("sklearn_available") else "未安装", border=True)

if scoring_mode == SCORING_RULE_FALLBACK:
    st.error("ML 评分未生效，当前为**规则降级**打分。降级不等于报错，但评分质量会下降。")
elif scoring_mode == SCORING_DISABLED:
    st.info("ML 评分处于**显式关闭**状态（配置选择，不是故障）。")
elif scoring_mode == SCORING_ML:
    st.success("ML 模型已加载并生效。")


st.subheader("ML 打分开关（按租户）")
st.caption(
    "**默认 `rule`（不显式开启就不上 ML）**：装依赖与开启 ML 放在最后，先保证功能完整。"
    f"当前租户：`{page_tenant_id or '未识别'}`。切换只影响本租户，不影响其他租户。"
)
_configured_mode = resolve_scoring_mode(store, page_tenant_id)
_selected_mode = st.radio(
    "打分方式",
    options=[MODE_RULE, MODE_ML],
    index=0 if _configured_mode == MODE_RULE else 1,
    format_func=lambda value: MODE_LABELS.get(value, value),
    horizontal=True,
    disabled=not admin,
    key="ml_scoring_mode_switch",
)
if _selected_mode != _configured_mode:
    if not admin:
        st.warning("需要管理员权限才能修改打分开关。")
    elif not page_tenant_id:
        st.error("无法解析当前租户，已拒绝写全局开关（会连带切换其他租户）。")
    else:
        try:
            written_key = set_scoring_mode(store, page_tenant_id, _selected_mode)
        except ValueError as exc:
            st.error(str(exc))
        else:
            store.audit(
                "ML_SCORING_MODE_CHANGED",
                {
                    "tenant_id": page_tenant_id,
                    "before": _configured_mode,
                    "after": _selected_mode,
                    "key": written_key,
                },
                actor="streamlit_admin:panel",
            )
            st.rerun()


st.subheader("ML 配置（按租户）")
st.caption(
    "这些值全部走**租户级三层解析**（租户键 → 全局键 → 默认），改动只影响当前租户，"
    "并写审计事件 `ML_FACTOR_CHANGED`。非法值会被拒绝，不会静默生效。"
)
with st.form("ml_factor_form", border=True):
    _current_values = {
        name: resolve_ml_factor(store, page_tenant_id, name) for name in FACTOR_SPECS
    }
    _inputs = {}
    for _name, _spec in FACTOR_SPECS.items():
        if _spec["kind"] == "str":
            _inputs[_name] = st.text_input(
                _spec["label"], value=str(_current_values[_name]), disabled=not admin,
            )
        else:
            _inputs[_name] = st.number_input(
                _spec["label"],
                value=float(_current_values[_name]),
                min_value=float(_spec["min"]), max_value=float(_spec["max"]),
                step=1.0 if _spec["kind"] == "int" else 0.01,
                disabled=not admin,
            )
    _saved = st.form_submit_button("保存 ML 配置", type="primary", disabled=not admin)
if _saved:
    if not page_tenant_id:
        st.error("无法解析当前租户，已拒绝写全局配置。")
    else:
        _changed: list[dict] = []
        for _name, _value in _inputs.items():
            try:
                _key = set_ml_factor(store, page_tenant_id, _name, _value)
            except (ValueError, KeyError) as exc:
                st.error(f"{_name}: {exc}")
                continue
            _changed.append({"factor": _name, "value": _value, "key": _key})
        if _changed:
            store.audit(
                "ML_FACTOR_CHANGED",
                {"tenant_id": page_tenant_id, "changes": _changed},
                actor="streamlit_admin:panel",
            )
            st.success(f"已保存 {len(_changed)} 项（仅影响租户 {page_tenant_id}）。")



st.subheader("对照视图（按租户）")
st.caption(
    "PRD 区块④：**实盘跑 ML、模拟盘跑规则**（或反之），直接量出 ML 的增量。"
    "当前 ML 默认未开启（装依赖与开启按计划放最后），所以这里先给出「谁在用什么」，"
    "开启后同一张表就是对照结论的入口。"
)
from futu_quant.tenant import active_tenants as _active_tenants

_rows = []
for _profile in _active_tenants():
    _mode = resolve_scoring_mode(store, _profile.tenant_id)
    _rows.append(
        {
            "租户": _profile.tenant_id,
            "环境": getattr(_profile, "trading_env", "?"),
            "打分方式": MODE_LABELS.get(_mode, _mode),
            "模型路径": resolve_ml_factor(store, _profile.tenant_id, "model_path"),
            "打分阈值": resolve_ml_factor(store, _profile.tenant_id, "ml_threshold"),
        }
    )
if _rows:
    st.dataframe(pd.DataFrame(_rows), hide_index=True, width="stretch")
    _ml_tenants = [r["租户"] for r in _rows if r["打分方式"] == MODE_LABELS.get(MODE_ML, MODE_ML)]
    if not _ml_tenants:
        st.info("目前没有任何租户启用 ML 打分 —— 对照尚未开始（这是预期状态，不是故障）。")
    else:
        st.success(f"已启用 ML 的租户：{', '.join(_ml_tenants)}；其余租户走规则打分，可直接对照。")

st.subheader("重训控制")
st.caption(
    "手动触发会在**后台进程**里跑（不卡界面、不占服务主循环），并发触发由文件锁挡住。"
    "缺 `sklearn` 时结果是 `SKIP:sklearn_missing`（依赖安装按计划放最后，这是预期状态，不是错误）。"
)
_confirm = st.checkbox("我确认要触发一次重训（可能替换生产模型）", disabled=not admin)
if st.button("手动触发重训", type="primary", disabled=not admin or not _confirm):
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path as _Path

    _wrapper = _Path(__file__).resolve().parents[1] / "scripts" / "run_ml_retrain_wrapper.py"
    try:
        _sp.Popen(
            [_sys.executable, str(_wrapper), "--triggered-by", "panel", "--kind", "manual"],
            cwd=str(_wrapper.parents[1]),
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        st.success("已提交后台重训，结果会出现在下方历史表（刷新页面可见）。")
    except Exception as exc:  # noqa: BLE001 - 触发失败要说清，不静默
        st.error(f"触发失败：{exc}")

st.subheader("重训历史")
_runs = store.ml_retrain_runs(limit=20)
if _runs:
    st.dataframe(pd.DataFrame(_runs), hide_index=True, width="stretch")
else:
    st.info("还没有重训记录。SKIP（缺依赖/已在跑）也会记录在这里。")

st.subheader("为什么是这个状态")
detail_rows = [
    ["模型路径", health.get("model_path") or "—"],
    ["模型是否存在于磁盘", "是" if health.get("model_exists") else "否"],
    ["训练时间（pkl 内）", health.get("trained_at") or "未记录"],
    [
        "年龄来源",
        {
            "pkl.trained_at": "pkl 内 trained_at（可信）",
            "pkl.mtime": "pkl 文件修改时间（回落，可读但弱）",
            "unknown": "未知 —— 读不到模型",
        }.get(str(health.get("age_source") or "unknown"), str(health.get("age_source"))),
    ],
    ["新鲜度档位", FRESHNESS_LABELS.get(freshness, freshness or "未知")],
    ["打分方式", SCORING_LABELS.get(scoring_mode, scoring_mode or "未知")],
    ["判定原因", health.get("scoring_reason") or "—"],
    ["降级原因", health.get("degradation_cause") or "—"],
    ["元数据 json 的 trained_at", health.get("metadata_json_trained_at") or "无"],
]
st.dataframe(
    pd.DataFrame(detail_rows, columns=["字段", "值"]),
    hide_index=True, width="stretch",
)
st.caption(
    "⚠️ **元数据 json 的 `trained_at` 是「元数据刷新时间」，不是训练时间** —— "
    "历史脚本只刷它、从不训练，用它当年龄依据会让守卫永远显示新鲜。"
    f"本页年龄一律取 pkl：{freshness_summary_line(health)}"
)

st.subheader("这一页的判据来源")
st.markdown(
    "- 年龄来源优先级：**pkl 内 `trained_at` → pkl mtime**（绝不读 json 的 `trained_at`）；\n"
    "- 新鲜度阈值与告警**同源**：<30 绿 / 30–34 橙 / ≥35 红（同一套常量，页面与告警不得各写一份）；\n"
    "- 「显式禁用」与「静默降级」分开显示：前者是配置选择，不告警；后者必须可见，"
    "并由调度侧发 `ML_SCORING_DEGRADED` 告警。"
)
