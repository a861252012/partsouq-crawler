from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SEED = "https://partsouq.com/en/catalog/genuine"


@dataclass(frozen=True, slots=True)
class PartSouqMySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3308
    database: str = "partsouq"
    user: str = "partsouq"
    password: str = "partsouq-local"
    pool_min_size: int = 1
    pool_max_size: int = 10
    connect_timeout_seconds: int = 10

    @classmethod
    def from_env(cls, **overrides: object) -> PartSouqMySQLConfig:
        values: dict[str, object] = {
            "host": os.getenv("PARTSOUQ_MYSQL_HOST", "127.0.0.1"),
            "port": int(os.getenv("PARTSOUQ_MYSQL_PORT", "3308")),
            "database": os.getenv("PARTSOUQ_MYSQL_DATABASE", "partsouq"),
            "user": os.getenv("PARTSOUQ_MYSQL_USER", "partsouq"),
            "password": os.getenv("PARTSOUQ_MYSQL_PASSWORD", "partsouq-local"),
            "pool_min_size": int(os.getenv("PARTSOUQ_MYSQL_POOL_MIN_SIZE", "1")),
            "pool_max_size": int(os.getenv("PARTSOUQ_MYSQL_POOL_MAX_SIZE", "10")),
            "connect_timeout_seconds": int(
                os.getenv("PARTSOUQ_MYSQL_CONNECT_TIMEOUT_SECONDS", "10")
            ),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            host=str(values["host"]),
            port=int(str(values["port"])),
            database=str(values["database"]),
            user=str(values["user"]),
            password=str(values["password"]),
            pool_min_size=int(str(values["pool_min_size"])),
            pool_max_size=int(str(values["pool_max_size"])),
            connect_timeout_seconds=int(str(values["connect_timeout_seconds"])),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.host or not self.database or not self.user:
            raise ValueError("MySQL host, database, and user are required")
        if not 1 <= self.port <= 65535:
            raise ValueError("MySQL port must be between 1 and 65535")
        if self.pool_min_size < 1 or self.pool_max_size < self.pool_min_size:
            raise ValueError("MySQL pool sizes are invalid")
        if self.connect_timeout_seconds < 1:
            raise ValueError("MySQL connect timeout must be positive")

    def public_dsn(self) -> str:
        return f"mysql://{self.user}@{self.host}:{self.port}/{self.database}"

    def public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "pool_min_size": self.pool_min_size,
            "pool_max_size": self.pool_max_size,
            "connect_timeout_seconds": self.connect_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    database: Path = Path("mysql")
    concurrency: int = 1
    delay_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    max_pages: int = 0
    max_depth: int = 0
    user_agent: str = ""
    robots_policy: str = "require"
    lease_seconds: int = 120
    log_json: bool = False
    transport: str = "http"
    browser_executable: Path | None = None
    browser_headless: bool = False

    @classmethod
    def from_env(cls, **overrides: object) -> CrawlerConfig:
        values: dict[str, object] = {
            "database": Path("mysql"),
            "concurrency": int(os.getenv("PARTSOUQ_CONCURRENCY", "1")),
            "delay_seconds": float(os.getenv("PARTSOUQ_DELAY_SECONDS", "5")),
            "request_timeout_seconds": float(os.getenv("PARTSOUQ_REQUEST_TIMEOUT_SECONDS", "30")),
            "max_retries": int(os.getenv("PARTSOUQ_MAX_RETRIES", "3")),
            "user_agent": os.getenv("PARTSOUQ_USER_AGENT", ""),
            "transport": os.getenv("PARTSOUQ_TRANSPORT", "http"),
            "browser_executable": os.getenv("PARTSOUQ_BROWSER_EXECUTABLE") or None,
            "browser_headless": os.getenv("PARTSOUQ_BROWSER_HEADLESS", "0").lower()
            in {"1", "true", "yes"},
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            database=Path(str(values["database"])),
            concurrency=int(str(values["concurrency"])),
            delay_seconds=float(str(values["delay_seconds"])),
            request_timeout_seconds=float(str(values["request_timeout_seconds"])),
            max_retries=int(str(values["max_retries"])),
            max_pages=int(str(values.get("max_pages", 0))),
            max_depth=int(str(values.get("max_depth", 0))),
            user_agent=str(values["user_agent"]),
            robots_policy=str(values.get("robots_policy", "require")),
            lease_seconds=int(str(values.get("lease_seconds", 120))),
            log_json=bool(values.get("log_json", False)),
            transport=str(values.get("transport", "http")),
            browser_executable=(
                Path(str(values["browser_executable"]))
                if values.get("browser_executable")
                else None
            ),
            browser_headless=bool(values.get("browser_headless", False)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.delay_seconds < 0:
            raise ValueError("delay must not be negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if self.max_pages < 0 or self.max_depth < 0:
            raise ValueError("max-pages and max-depth use 0 for unlimited")
        if self.robots_policy not in {"require", "ignore"}:
            raise ValueError("robots-policy must be require or ignore")
        if self.transport not in {"http", "browser"}:
            raise ValueError("transport must be http or browser")
        if self.transport == "browser" and self.concurrency != 1:
            raise ValueError("browser transport requires concurrency 1")

    def public_dict(self) -> dict[str, object]:
        return {
            "database_backend": "mysql",
            "concurrency": self.concurrency,
            "delay_seconds": self.delay_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "robots_policy": self.robots_policy,
            "lease_seconds": self.lease_seconds,
            "transport": self.transport,
            "browser_executable": (
                str(self.browser_executable) if self.browser_executable else None
            ),
            "browser_headless": self.browser_headless,
        }
