from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True, slots=True)
class AdminConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_user: str = "partsouq_admin"
    mysql_password: str = "partsouq-admin-local"
    mysql_database: str = "partsouq"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8086
    secret_key: str = ""
    username: str = ""
    password: str = ""
    secure_cookie: bool = False
    default_actor: str = "local-admin"
    page_size: int = 50

    @classmethod
    def from_env(cls) -> AdminConfig:
        return cls(
            mysql_host=os.getenv("PARTSOUQ_MYSQL_HOST", "127.0.0.1"),
            mysql_port=_env_int("PARTSOUQ_MYSQL_PORT", 3308),
            mysql_user=os.getenv("PARTSOUQ_ADMIN_MYSQL_USER", "partsouq_admin"),
            mysql_password=os.getenv(
                "PARTSOUQ_ADMIN_MYSQL_PASSWORD",
                "partsouq-admin-local",
            ),
            mysql_database=os.getenv("PARTSOUQ_MYSQL_DATABASE", "partsouq"),
            bind_host=os.getenv("PARTSOUQ_ADMIN_HOST", "127.0.0.1"),
            bind_port=_env_int("PARTSOUQ_ADMIN_PORT", 8086),
            secret_key=os.getenv("PARTSOUQ_ADMIN_SECRET_KEY", ""),
            username=os.getenv("PARTSOUQ_ADMIN_USERNAME", ""),
            password=os.getenv("PARTSOUQ_ADMIN_PASSWORD", ""),
            secure_cookie=os.getenv("PARTSOUQ_ADMIN_SECURE_COOKIE", "0") == "1",
            default_actor=os.getenv("PARTSOUQ_ADMIN_ACTOR", "local-admin"),
            page_size=min(max(_env_int("PARTSOUQ_ADMIN_PAGE_SIZE", 50), 1), 100),
        )

    def resolved_secret_key(self) -> str:
        return self.secret_key or secrets.token_hex(32)

    @property
    def auth_required(self) -> bool:
        return bool(self.username and self.password)

    def validate_server_mode(self) -> None:
        if bool(self.username) != bool(self.password):
            raise ValueError(
                "PARTSOUQ_ADMIN_USERNAME and PARTSOUQ_ADMIN_PASSWORD must be set together"
            )
        if self.auth_required and not self.secret_key:
            raise ValueError("authenticated admin requires PARTSOUQ_ADMIN_SECRET_KEY")
        if self.bind_host in {"127.0.0.1", "localhost", "::1"}:
            return
        if not self.auth_required:
            raise ValueError("non-loopback admin requires PARTSOUQ_ADMIN_USERNAME/PASSWORD")
        if not self.secure_cookie:
            raise ValueError("non-loopback admin requires PARTSOUQ_ADMIN_SECURE_COOKIE=1")
