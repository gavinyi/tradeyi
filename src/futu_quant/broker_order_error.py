"""从券商订单推送/回报报文里提取原始错误，落库可追溯。

背景（2026-10-27 实盘 $0.01 探针事故）：`id=892` 那笔单券商 0.37 秒回报
`FAILED`（source=BROKER_PUSH），但 `order_intents.error` 为空——券商给的
失败原因完全丢了。这比状态标签更严重：等于瞎跑。

本模块只做**纯解析**，不碰状态机、不动下单门禁顺序，便于单测与守卫。
券商没给原文时**绝不编造**，只写「券商未返回原因」并保留原始报文字段名，
让后续能核对报文到底有没有错误字段。
"""

from __future__ import annotations

import json

# 券商报文中可能出现的错误字段（不同 API 版本/推送类型字段名会变，
# 用候选清单兜底，缺失即视为「未返回」）。
_ERROR_CODE_CANDIDATES = ("err_code", "last_err_code", "order_err_code")
_ERROR_TEXT_CANDIDATES = ("last_err_msg", "remark", "err_msg", "error", "err_desc")


def extract_broker_order_error(row: object | None) -> dict:
    """从一行券商订单报文里提取错误原文。

    接受 ``dict`` 或 ``pandas.Series``（券商推送 `on_recv_rsp` 里每行是
    Series），Series 在入口先 ``to_dict()`` 归一，保证生产链路也能取到原文。
    也接受任意 Mapping；既不是 dict 也没有 ``to_dict`` 的，按「未返回原因」处理。

    返回 dict：
    - ``code``：数字/字符串错误码（报文里存在才给，否则空串）
    - ``text``：错误文本（``last_err_msg`` / ``remark`` 等，否则空串）
    - ``raw``：所有候选字段在报文里的原始值（字段名原样保留，便于核对报文）
    - ``has_reason``：报文里是否真的给了错误码或错误文本

    解析失败一律按「未返回原因」处理（fail-safe：宁可留空也不编一个错误）。
    """
    raw: dict[str, object] = {}

    if row is None or not hasattr(row, "get"):
        return {"code": "", "text": "", "raw": raw, "has_reason": False}
    # 券商推送的每行是 pandas Series，归一成 dict 再查字段。
    if not isinstance(row, dict) and hasattr(row, "to_dict"):
        row = row.to_dict()

    for key in (*_ERROR_CODE_CANDIDATES, *_ERROR_TEXT_CANDIDATES):
        if key in row:
            raw[key] = row.get(key)

    code = ""
    for key in _ERROR_CODE_CANDIDATES:
        value = row.get(key)
        if value not in (None, "", 0):
            code = str(value)
            break

    text = ""
    for key in _ERROR_TEXT_CANDIDATES:
        value = row.get(key)
        if value not in (None, ""):
            text = str(value)
            break

    return {
        "code": code,
        "text": text,
        "raw": raw,
        "has_reason": bool(code) or bool(text),
    }


def format_broker_order_error(row: object | None) -> str:
    """把券商错误原文格式化成一行可落 ``order_intents.error`` 的字符串。

    规则（不编造）：
    - 错误码 + 文本 → ``错误码 <code>：<text>``
    - 只有错误码 → ``错误码 <code>（券商未提供错误文本）``
    - 只有文本 → ``券商错误：<text>``
    - 两者都无 → ``券商未返回原因``，并把查过的报文字段名附上，便于核对报文
    """
    extracted = extract_broker_order_error(row)
    code, text = extracted["code"], extracted["text"]
    if code and text:
        return f"错误码 {code}：{text}"
    if code:
        return f"错误码 {code}（券商未提供错误文本）"
    if text:
        return f"券商错误：{text}"
    checked = ",".join((*_ERROR_CODE_CANDIDATES, *_ERROR_TEXT_CANDIDATES))
    return f"券商未返回原因（已查字段: {checked}）"


def error_details_json(row: object | None) -> str:
    """生成可落 ``order_state_events.details_json`` 的 JSON 字符串。

    原样保留报文错误字段（字段名不重命名），同时附带格式化文案。
    """
    extracted = extract_broker_order_error(row)
    payload = {
        "broker_error": extracted["raw"],
        "formatted": format_broker_order_error(row),
        "has_reason": extracted["has_reason"],
    }
    return json.dumps(payload, ensure_ascii=False)
