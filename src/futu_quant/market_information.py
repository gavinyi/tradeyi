from __future__ import annotations

from dataclasses import dataclass
import json
from math import log1p
import os
import subprocess
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from .data_quality import quote_freshness


BENCHMARK_SYMBOLS = ("US.SPY", "US.QQQ", "US.DIA", "US.IWM")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def combined_watchlist(settings, held_symbols: Iterable[str] = ()) -> pd.DataFrame:
    groups = (
        ("普通策略", settings.watchlist),
        ("Core Satellite", settings.core_satellite_symbols),
        ("财报 Gamma", settings.gamma_symbols),
        ("波动率期权", settings.volatility_symbols),
    )
    membership: dict[str, list[str]] = {}
    held = {str(symbol).upper() for symbol in held_symbols}
    for label, symbols in groups:
        for symbol in symbols:
            code = str(symbol).upper()
            membership.setdefault(code, [])
            if label not in membership[code]:
                membership[code].append(label)
    for symbol in held:
        membership.setdefault(symbol, [])
    rows = [
        {
            "代码": symbol,
            "来源": "、".join(membership[symbol]) or "当前持仓",
            "当前持仓": "是" if symbol in held else "否",
        }
        for symbol in sorted(membership)
    ]
    return pd.DataFrame(rows, columns=["代码", "来源", "当前持仓"])


def _number(frame: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    if column not in frame:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").fillna(default)


def normalize_market_snapshots(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "代码", "名称", "最新价", "涨跌额", "涨跌幅", "开盘价", "最高价", "最低价",
        "日内振幅", "买一价", "卖一价", "买卖价差", "成交量", "成交额",
        "换手率", "更新时间", "行情状态", "行情说明",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(index=frame.index)
    result["代码"] = frame.get("code", pd.Series("", index=frame.index)).astype(str)
    result["名称"] = frame.get("name", pd.Series("", index=frame.index)).astype(str)
    last = _number(frame, "last_price")
    previous = _number(frame, "prev_close_price")
    high = _number(frame, "high_price")
    low = _number(frame, "low_price")
    bid = _number(frame, "bid_price")
    ask = _number(frame, "ask_price")
    mid = (bid + ask) / 2
    result["最新价"] = last
    result["涨跌额"] = last - previous
    result["涨跌幅"] = ((last / previous) - 1).where(previous > 0)
    result["开盘价"] = _number(frame, "open_price")
    result["最高价"] = high
    result["最低价"] = low
    result["日内振幅"] = ((high - low) / previous).where(previous > 0)
    result["买一价"] = bid
    result["卖一价"] = ask
    result["买卖价差"] = ((ask - bid) / mid).where(mid > 0)
    result["成交量"] = _number(frame, "volume")
    result["成交额"] = _number(frame, "turnover")
    result["换手率"] = _number(frame, "turnover_rate")
    result["更新时间"] = frame.get(
        "update_time", pd.Series("", index=frame.index)
    ).astype(str)
    statuses = []
    reasons = []
    for _, row in frame.iterrows():
        fresh, reason, age = quote_freshness(row, max_age_seconds=180)
        statuses.append("新鲜" if fresh else "时间异常" if age == float("inf") else "延迟")
        reasons.append(reason)
    result["行情状态"] = statuses
    result["行情说明"] = reasons
    return result.reset_index(drop=True)[columns]


def summarize_market_breadth(frame: pd.DataFrame) -> dict[str, float | int]:
    values = pd.to_numeric(frame.get("涨跌幅", pd.Series(dtype=float)), errors="coerce").dropna()
    up = int((values > 0).sum())
    down = int((values < 0).sum())
    flat = int((values == 0).sum())
    directional = up + down
    return {
        "上涨数": up,
        "下跌数": down,
        "平盘数": flat,
        "上涨占比": up / directional if directional else 0.0,
        "平均涨跌幅": float(values.mean()) if not values.empty else 0.0,
    }


def rank_option_market(frame: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Add comparable liquidity and moneyness fields to an option-chain snapshot."""
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    bid = _number(result, "bid_price")
    ask = _number(result, "ask_price")
    mid = (bid + ask) / 2
    strike = _number(result, "strike_price")
    open_interest = _number(result, "option_open_interest")
    volume = _number(result, "volume")
    result["中间价"] = mid
    result["买卖价差"] = ((ask - bid) / mid).where(mid > 0)
    result["距平值幅度"] = ((strike / float(spot)) - 1).abs() if spot > 0 else float("nan")
    result["流动性评分"] = (
        open_interest.map(log1p) * 12
        + volume.map(log1p) * 8
        - result["买卖价差"].fillna(1.0) * 100
        - result["距平值幅度"].fillna(1.0) * 20
    ).clip(lower=0, upper=100)
    return result.sort_values(
        ["流动性评分", "买卖价差", "距平值幅度"],
        ascending=[False, True, True],
    ).reset_index(drop=True)


def parse_recent_sec_filings(
    payload: dict, forms: set[str] | None = None, limit: int = 30,
) -> list[dict]:
    recent = payload.get("filings", {}).get("recent", {})
    cik = str(payload.get("cik", "")).zfill(10)
    accessions = recent.get("accessionNumber", [])
    rows = []
    for index, accession in enumerate(accessions):
        form = str(recent.get("form", [""] * len(accessions))[index])
        if forms and form not in forms:
            continue
        document = str(recent.get("primaryDocument", [""] * len(accessions))[index])
        accession_path = str(accession).replace("-", "")
        link = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_path}/{document}"
        )
        rows.append({
            "表格类型": form,
            "提交日期": str(recent.get("filingDate", [""] * len(accessions))[index]),
            "报告日期": str(recent.get("reportDate", [""] * len(accessions))[index]),
            "受理时间": str(
                recent.get("acceptanceDateTime", [""] * len(accessions))[index]
            ),
            "说明": str(
                recent.get("primaryDocDescription", [""] * len(accessions))[index]
            ),
            "公告链接": link,
            "受理编号": str(accession),
        })
        if len(rows) >= max(1, int(limit)):
            break
    return rows


@dataclass(frozen=True)
class SecCompany:
    ticker: str
    cik: str
    name: str
    exchange: str


class SecFilingsClient:
    """Small, on-demand EDGAR client; callers should cache results and avoid bulk scans."""

    def __init__(self, user_agent: str | None = None, timeout: float = 12.0):
        self.user_agent = user_agent or os.getenv(
            "SEC_USER_AGENT",
            "Mozilla/5.0 FutuPaperQuant/1.0 contact@futupaperquant.local",
        )
        self.timeout = max(1.0, float(timeout))

    def _json(self, url: str) -> dict:
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "identity",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as first_error:
            # Some macOS Python/OpenSSL combinations are rejected during the SEC/Akamai
            # TLS handshake while the system TLS stack used by curl remains compatible.
            try:
                completed = subprocess.run(
                    [
                        "curl", "--silent", "--show-error", "--fail",
                        "--max-time", str(int(self.timeout)),
                        "--user-agent", self.user_agent, url,
                    ],
                    check=True, capture_output=True, text=True, timeout=self.timeout + 2,
                )
                return json.loads(completed.stdout)
            except (
                FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError,
            ) as fallback_error:
                raise RuntimeError(
                    f"SEC EDGAR 请求失败：{type(first_error).__name__}；"
                    f"系统 TLS 备用通道失败：{type(fallback_error).__name__}"
                ) from fallback_error

    def company(self, ticker: str) -> SecCompany:
        normalized = str(ticker).upper().removeprefix("US.")
        payload = self._json(SEC_TICKERS_URL)
        fields = payload.get("fields", [])
        for values in payload.get("data", []):
            row = dict(zip(fields, values))
            if str(row.get("ticker", "")).upper() == normalized:
                return SecCompany(
                    ticker=normalized,
                    cik=str(row.get("cik", "")).zfill(10),
                    name=str(row.get("name", "")),
                    exchange=str(row.get("exchange", "")),
                )
        raise ValueError(f"SEC 公司目录中没有找到 {normalized}")

    def recent_filings(
        self, ticker: str, forms: set[str] | None = None, limit: int = 30,
    ) -> tuple[SecCompany, list[dict]]:
        company = self.company(ticker)
        payload = self._json(SEC_SUBMISSIONS_URL.format(cik=company.cik))
        return company, parse_recent_sec_filings(payload, forms=forms, limit=limit)
