from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NHTSA_API_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class NhtsaConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_database: str = "nhtsa"
    mysql_user: str = "nhtsa"
    mysql_password: str = "nhtsa-local"
    raw_dir: Path = Path("output/nhtsa/raw")
    user_agent: str = "nhtsa-official-data-sync/0.1"
    request_timeout_seconds: float = 120.0
    api_delay_seconds: float = DEFAULT_NHTSA_API_DELAY_SECONDS

    @classmethod
    def from_env(cls, **overrides: object) -> NhtsaConfig:
        values: dict[str, object] = {
            "mysql_host": os.getenv("NHTSA_MYSQL_HOST", "127.0.0.1"),
            "mysql_port": int(os.getenv("NHTSA_MYSQL_PORT", "3308")),
            "mysql_database": os.getenv("NHTSA_MYSQL_DATABASE", "nhtsa"),
            "mysql_user": os.getenv("NHTSA_MYSQL_USER", "nhtsa"),
            "mysql_password": os.getenv("NHTSA_MYSQL_PASSWORD", "nhtsa-local"),
            "raw_dir": Path(os.getenv("NHTSA_RAW_DIR", "output/nhtsa/raw")),
            "user_agent": os.getenv("NHTSA_USER_AGENT", "nhtsa-official-data-sync/0.1"),
            "request_timeout_seconds": float(os.getenv("NHTSA_REQUEST_TIMEOUT_SECONDS", "120")),
            "api_delay_seconds": float(
                os.getenv("NHTSA_API_DELAY_SECONDS", str(DEFAULT_NHTSA_API_DELAY_SECONDS))
            ),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            mysql_host=str(values["mysql_host"]),
            mysql_port=int(str(values["mysql_port"])),
            mysql_database=str(values["mysql_database"]),
            mysql_user=str(values["mysql_user"]),
            mysql_password=str(values["mysql_password"]),
            raw_dir=Path(str(values["raw_dir"])),
            user_agent=str(values["user_agent"]),
            request_timeout_seconds=float(str(values["request_timeout_seconds"])),
            api_delay_seconds=float(str(values["api_delay_seconds"])),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.mysql_host or not self.mysql_database or not self.mysql_user:
            raise ValueError("NHTSA MySQL host, database, and user are required")
        if not 1 <= self.mysql_port <= 65535:
            raise ValueError("NHTSA MySQL port must be between 1 and 65535")
        if self.request_timeout_seconds <= 0:
            raise ValueError("NHTSA request timeout must be positive")
        if self.api_delay_seconds < 0:
            raise ValueError("NHTSA API delay must not be negative")
        if not self.user_agent.strip():
            raise ValueError("NHTSA user agent must not be empty")

    def public_dict(self) -> dict[str, object]:
        return {
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "raw_dir": str(self.raw_dir),
            "request_timeout_seconds": self.request_timeout_seconds,
            "api_delay_seconds": self.api_delay_seconds,
        }
