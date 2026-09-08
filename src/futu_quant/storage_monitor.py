from __future__ import annotations

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def storage_report(root: Path | str = ROOT) -> dict:
    root = Path(root)
    total, used, free = shutil.disk_usage(root)
    components = {
        "database": _tree_size(root / "data") - _tree_size(root / "data" / "warehouse")
                    - _tree_size(root / "data" / "backups") - _tree_size(root / "data" / "recovery"),
        "parquet": _tree_size(root / "data" / "warehouse"),
        "backups": _tree_size(root / "data" / "backups"),
        "recovery": _tree_size(root / "data" / "recovery"),
        "logs": sum(path.stat().st_size for path in (root / "data").glob("*.log")),
    }
    warning_free = max(5 * 1024**3, total * 0.10)
    return {"total_bytes": total, "used_bytes": used, "free_bytes": free,
            "free_ratio": free / total if total else 0, "status": "WARNING" if free < warning_free else "HEALTHY",
            "components": components, "warning_threshold_bytes": warning_free}
