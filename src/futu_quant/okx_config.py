from __future__ import annotations

from dataclasses import dataclass
import os

from .secret_store import get_secret


OKX_API_BASE_URLS = (
    "https://openapi.okx.com",
    "https://us.okx.com",
    "https://eea.okx.com",
    "https://tr.okx.com",
)
LEGACY_OKX_BASE_URLS = {
    "https://www.okx.com": "https://openapi.okx.com",
    "https://app.okx.com": "https://us.okx.com",
}


def normalize_okx_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        return OKX_API_BASE_URLS[0]
    return LEGACY_OKX_BASE_URLS.get(normalized, normalized)


@dataclass(frozen=True)
class OkxSettings:
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    base_url: str = "https://openapi.okx.com"
    trading_mode: str = "DEMO"
    enable_demo_orders: bool = False
    timeout_seconds: float = 10.0
    retry_attempts: int = 2
    retry_backoff_seconds: float = 0.8
    max_order_notional: float = 500.0

    def __post_init__(self):
        object.__setattr__(self, "base_url", normalize_okx_base_url(self.base_url))
        if self.trading_mode.upper() != "DEMO":
            raise ValueError("安全锁：当前版本只支持欧易模拟盘 DEMO")
        if self.base_url not in OKX_API_BASE_URLS:
            raise ValueError("OKX_BASE_URL 必须是官方 HTTPS API 域名")
        if self.timeout_seconds <= 0 or self.max_order_notional <= 0:
            raise ValueError("欧易超时和单笔金额上限必须大于 0")
        if self.retry_attempts < 0 or self.retry_backoff_seconds < 0:
            raise ValueError("欧易重试次数和退避时间不能小于 0")

    @property
    def credentials_configured(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    @classmethod
    def from_env(cls) -> "OkxSettings":
        return cls(
            api_key=get_secret("OKX_API_KEY").strip(),
            secret_key=get_secret("OKX_SECRET_KEY").strip(),
            passphrase=get_secret("OKX_PASSPHRASE").strip(),
            base_url=normalize_okx_base_url(
                os.getenv("OKX_BASE_URL", "https://openapi.okx.com")
            ),
            trading_mode=os.getenv("OKX_TRADING_MODE", "DEMO").strip().upper(),
            enable_demo_orders=os.getenv("OKX_ENABLE_DEMO_ORDERS", "false").lower() == "true",
            timeout_seconds=float(os.getenv("OKX_TIMEOUT_SECONDS", "10")),
            retry_attempts=int(os.getenv("OKX_RETRY_ATTEMPTS", "2")),
            retry_backoff_seconds=float(os.getenv("OKX_RETRY_BACKOFF_SECONDS", "0.8")),
            max_order_notional=float(os.getenv("OKX_MAX_ORDER_NOTIONAL", "500")),
        )
