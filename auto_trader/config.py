"""Environment configuration for the trader.

The project intentionally keeps configuration small in phase 2.  A tiny
``.env`` loader is used so the application can run with the existing project
dependencies; real secrets still stay in the ignored ``.env`` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_local_env(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overwriting process variables."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TossConfig:
    """Non-secret connection settings for the Toss Securities API."""

    base_url: str
    ws_url: str
    client_id: str
    client_secret: str
    account_seq: str
    market_data_enabled: bool
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "TossConfig":
        return cls(
            base_url=os.getenv("TOSS_API_BASE_URL", "https://openapi.tossinvest.com").rstrip("/"),
            ws_url=os.getenv("TOSS_WS_URL", "wss://openapi-ws.tossinvest.com/ws/v1"),
            client_id=os.getenv("TOSS_CLIENT_ID", "").strip(),
            client_secret=os.getenv("TOSS_CLIENT_SECRET", "").strip(),
            account_seq=os.getenv("TOSS_ACCOUNT_SEQ", "").strip(),
            market_data_enabled=_env_bool("TOSS_MARKET_DATA_ENABLED", False),
            timeout_seconds=float(os.getenv("TOSS_API_TIMEOUT_SECONDS", "10")),
        )

    @property
    def credentials_configured(self) -> bool:
        placeholders = {"", "your_toss_client_id", "your_toss_client_secret"}
        return self.client_id not in placeholders and self.client_secret not in placeholders

