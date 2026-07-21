from __future__ import annotations

import json

from futu_quant.backup_recovery import verify_backup_bundle
from futu_quant.store import TradingStore


def test_backup_bundle_can_be_restored(tmp_path):
    source = TradingStore(tmp_path / "source.db")
    source.set_setting("execution_mode", "PAPER")
    backup_dir = tmp_path / "backups"
    database = source.backup(backup_dir / "trading.db")
    config = backup_dir / "config.env"
    config.write_text("FUTU_TRADING_ENV=SIMULATE\n")
    manifest = backup_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "database": database.name, "configuration": config.name,
    }))

    result = verify_backup_bundle(manifest, tmp_path / "restored")

    assert result["integrity"] == "ok"
    assert (tmp_path / "restored" / ".env").exists()
