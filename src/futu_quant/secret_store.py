from __future__ import annotations

import os
import platform
import subprocess


SERVICE = "futu-paper-quant"


def keychain_available() -> bool:
    return platform.system() == "Darwin" and bool(subprocess.run(
        ["which", "security"], capture_output=True, check=False,
    ).stdout.strip())


def set_secret(name: str, value: str) -> None:
    if not keychain_available():
        raise RuntimeError("当前系统钥匙串不可用；继续使用受权限保护的环境变量")
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", name, "-s", SERVICE, "-w", value],
        check=True, capture_output=True,
    )


def get_secret(name: str, default: str = "") -> str:
    if keychain_available():
        result = subprocess.run(
            ["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return os.getenv(name, default)


def secret_status(names: list[str]) -> list[dict]:
    return [{"name": name, "configured": bool(get_secret(name)),
             "source": "系统钥匙串" if keychain_available() and bool(get_secret(name)) else "环境变量/未配置"}
            for name in names]
