from __future__ import annotations

from pathlib import Path
import tempfile

from futu_quant.backup_recovery import verify_backup_bundle


def main() -> None:
    manifests = sorted(Path("data/backups").glob("manifest-*.json"))
    if not manifests:
        raise RuntimeError("没有可验证的备份清单")
    with tempfile.TemporaryDirectory(prefix="futu-restore-") as directory:
        print(verify_backup_bundle(manifests[-1], directory))


if __name__ == "__main__":
    main()
