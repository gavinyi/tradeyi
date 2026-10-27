from __future__ import annotations

import json

import pandas as pd

from futu_quant.broker_order_error import (
    extract_broker_order_error,
    format_broker_order_error,
    error_details_json,
)
from futu_quant.store import TradingStore


def test_error_code_and_text_formatted():
    row = {"order_id": "FH1", "order_status": "FAILED",
           "err_code": 3001, "last_err_msg": "账户无交易权限"}
    assert format_broker_order_error(row) == "错误码 3001：账户无交易权限"


def test_only_error_code_formatted():
    row = {"order_id": "FH2", "order_status": "FAILED", "err_code": 3001}
    assert format_broker_order_error(row) == "错误码 3001（券商未提供错误文本）"


def test_only_error_text_formatted():
    row = {"order_id": "FH3", "order_status": "FAILED", "remark": "该证券代码不可交易"}
    assert format_broker_order_error(row) == "券商错误：该证券代码不可交易"


def test_no_reason_records_checked_fields():
    row = {"order_id": "FH4", "order_status": "FAILED"}
    msg = format_broker_order_error(row)
    assert msg.startswith("券商未返回原因（已查字段:")
    assert "err_code" in msg and "last_err_msg" in msg


def test_field_name_variation_caught():
    # 不同 API 版本/推送类型错误字段名会变，候选清单必须兜底
    row = {"order_id": "FH5", "order_status": "FAILED", "order_err_code": 5001,
           "err_msg": "系统繁忙"}
    assert format_broker_order_error(row) == "错误码 5001：系统繁忙"


def test_extract_from_real_pandas_series():
    # 生产链路：order_push.on_recv_rsp 里每行是 pandas Series，
    # 入口必须把 Series 当 dict 处理，否则永远返回「未返回原因」（功能失效）。
    row = pd.Series({"order_id": "FH7", "order_status": "FAILED",
                     "err_code": 3001, "last_err_msg": "账户无交易权限"})
    extracted = extract_broker_order_error(row)
    assert extracted["has_reason"] is True
    assert extracted["code"] == "3001"
    assert extracted["text"] == "账户无交易权限"
    assert format_broker_order_error(row) == "错误码 3001：账户无交易权限"


def test_series_without_reason_still_no_fabrication():
    # 没有错误字段的 Series，依然不编造
    row = pd.Series({"order_id": "FH8", "order_status": "SUBMITTED"})
    extracted = extract_broker_order_error(row)
    assert extracted["has_reason"] is False
    assert format_broker_order_error(row).startswith("券商未返回原因")


def test_none_row_is_no_reason():
    assert format_broker_order_error(None).startswith("券商未返回原因")


def test_legacy_field_name_last_err_msg_and_remark():
    # ui.py 里标注过这两个字段名，确认都能被解析
    assert "last_err_msg" in extract_broker_order_error(
        {"last_err_msg": "x"})["raw"]
    assert "remark" in extract_broker_order_error({"remark": "y"})["raw"]


def test_details_json_roundtrip():
    row = {"order_id": "FH6", "order_status": "FAILED",
           "err_code": 7, "remark": "测试"}
    payload = json.loads(error_details_json(row))
    assert payload["formatted"] == "错误码 7：测试"
    assert payload["has_reason"] is True
    assert payload["broker_error"]["err_code"] == 7
    assert payload["broker_error"]["remark"] == "测试"


def test_store_persists_broker_error_on_failed(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.create_intent(
        "strat", "key1", "US.AAPL", "BUY", 1, 0.01, metadata={},
    )
    intent = store.intents(limit=10)[0]
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id="FHX1")

    store.update_by_broker_order(
        "FHX1", "FAILED", 0, 0, 0, source="BROKER_PUSH",
        error="错误码 3001：账户无交易权限",
    )
    updated = store.intent_by_broker_order("FHX1")
    assert updated["status"] == "FAILED"
    assert updated["error"] == "错误码 3001：账户无交易权限"


def test_store_error_keeps_existing_when_none(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.create_intent(
        "strat", "key2", "US.AAPL", "BUY", 1, 0.01, metadata={},
    )
    intent = store.intents(limit=10)[0]
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id="FHX2",
                        error="原始原因")
    # 后续推送（如成交）不传 error，不应把已有错误清空
    store.update_by_broker_order("FHX2", "FILLED", 1, 0.01, 0, source="BROKER_PUSH")
    assert store.intent_by_broker_order("FHX2")["error"] == "原始原因"
