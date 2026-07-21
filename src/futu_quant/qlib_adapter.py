from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import normalize_us_symbol


@dataclass(frozen=True)
class QlibStatus:
    available: bool
    version: str
    message: str


class QlibResearchAdapter:
    """Optional boundary around Qlib; it never emits executable orders."""

    def __init__(self, importer: Callable[[str], object] = importlib.import_module):
        self._importer = importer

    def _module(self):
        return self._importer("qlib")

    def status(self) -> QlibStatus:
        try:
            module = self._module()
        except ImportError:
            return QlibStatus(
                False, "", "未安装 pyqlib；可选研究功能未启用，不影响交易系统",
            )
        version = str(getattr(module, "__version__", "unknown"))
        return QlibStatus(True, version, f"Qlib {version} 可用；仅生成研究信号")

    def initialize(self, provider_uri: Path | str, region: str = "us") -> None:
        module = self._module()
        module.init(provider_uri=str(provider_uri), region=region)

    def prepare_dataset(
        self, canonical_bars: pd.DataFrame, output_dir: Path | str, dataset_version: str,
    ) -> Path:
        required = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
        missing = sorted(required - set(canonical_bars.columns))
        if missing:
            raise ValueError(f"Qlib 研究数据缺少字段：{', '.join(missing)}")
        data = canonical_bars.copy()
        data["instrument"] = data["symbol"].astype(str).str.removeprefix("US.")
        data["datetime"] = pd.to_datetime(data["timestamp"], errors="coerce", utc=True).dt.tz_localize(None)
        data["factor"] = 1.0
        research = (
            data[["instrument", "datetime", "open", "high", "low", "close", "volume", "factor"]]
            .dropna(subset=["instrument", "datetime"])
            .set_index(["instrument", "datetime"])
            .sort_index()
        )
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        safe_version = "".join(character for character in dataset_version if character.isalnum() or character in "-_")
        path = root / f"qlib-research-{safe_version or 'unversioned'}.parquet"
        research.to_parquet(path, compression="zstd")
        return path

    def normalize_predictions(
        self, predictions: pd.Series | pd.DataFrame, model_version: str, dataset_version: str,
    ) -> pd.DataFrame:
        if isinstance(predictions, pd.DataFrame):
            if "score" not in predictions:
                if len(predictions.columns) != 1:
                    raise ValueError("Qlib 预测结果必须包含 score 字段")
                values = predictions.iloc[:, 0].rename("score")
            else:
                values = predictions["score"]
        else:
            values = predictions.rename("score")
        result = values.reset_index()
        if "instrument" not in result or "datetime" not in result:
            raise ValueError("Qlib 预测索引必须是 instrument / datetime")
        result["symbol"] = result["instrument"].astype(str).map(normalize_us_symbol)
        result["signal_time"] = pd.to_datetime(result["datetime"], errors="coerce", utc=True)
        result["score"] = pd.to_numeric(result["score"], errors="coerce")
        result["generated_at"] = datetime.now(timezone.utc)
        result["model_version"] = model_version
        result["dataset_version"] = dataset_version
        result["executable"] = False
        result["signal_type"] = "RESEARCH_ONLY"
        return result[
            ["symbol", "signal_time", "score", "generated_at", "model_version",
             "dataset_version", "executable", "signal_type"]
        ].dropna(subset=["signal_time", "score"])
