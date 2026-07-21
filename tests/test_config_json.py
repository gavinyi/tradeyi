from __future__ import annotations

import json

import numpy as np
import pandas as pd

from futu_quant.config import json_dumps


def test_config_json_supports_numpy_scalars_and_pandas_timestamps():
    encoded = json_dumps({
        "quantity": np.int64(3),
        "price": np.float64(1.25),
        "time": pd.Timestamp("2026-07-16 09:30:00"),
    })
    decoded = json.loads(encoded)

    assert decoded == {
        "quantity": 3, "price": 1.25, "time": "2026-07-16T09:30:00",
    }
