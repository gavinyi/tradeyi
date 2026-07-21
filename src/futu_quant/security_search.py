from __future__ import annotations

from difflib import SequenceMatcher

import pandas as pd

from .config import normalize_us_symbol


def search_securities(catalog: pd.DataFrame, query: str, exact: bool = False, limit: int = 30) -> pd.DataFrame:
    if catalog.empty or not query.strip():
        return catalog.head(0)
    if exact:
        code = normalize_us_symbol(query)
        return catalog[catalog["code"].astype(str).str.upper() == code]
    needle = query.strip().upper()

    def score(row: pd.Series) -> float:
        code = str(row.get("code", "")).upper()
        short_code = code.removeprefix("US.")
        name = str(row.get("name", "")).upper()
        searchable = f"{code} {short_code} {name}"
        if needle in {code, short_code}:
            return 1000
        if short_code.startswith(needle):
            return 800 - len(short_code)
        if needle in searchable:
            return 600 - searchable.index(needle)
        return max(
            SequenceMatcher(None, needle, short_code).ratio(),
            SequenceMatcher(None, needle, name).ratio(),
        ) * 100

    result = catalog.copy()
    result["_score"] = result.apply(score, axis=1)
    return result[result["_score"] >= 35].nlargest(limit, "_score").drop(columns="_score")


def security_label(row: pd.Series) -> str:
    name = str(row.get("name", "")).strip()
    return f"{row['code']} · {name}" if name else str(row["code"])
