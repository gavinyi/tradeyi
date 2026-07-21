from __future__ import annotations

import pandas as pd

from futu_quant.security_search import search_securities


def test_security_search_supports_exact_and_fuzzy_matches():
    catalog = pd.DataFrame([
        {"code": "US.NVDA", "name": "NVIDIA Corporation"},
        {"code": "US.AAPL", "name": "Apple Inc."},
    ])

    assert search_securities(catalog, "NVDA", exact=True).iloc[0]["code"] == "US.NVDA"
    assert search_securities(catalog, "nvid", exact=False).iloc[0]["code"] == "US.NVDA"
