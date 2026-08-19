"""User account storage for the login/registration feature.

This module owns the ``users`` table and nothing else.  It connects with a
dedicated ``ref_auth`` MySQL account that is granted SELECT/INSERT/UPDATE on
this single table — the training query path (``ref_reader``) is untouched.

Password hashing uses PBKDF2-SHA256 from the standard library; no external
dependencies are introduced.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Any

import pymysql


PBKDF2_ITERATIONS = 120_000
PHONE_RE_DIGITS = 11


class AuthStoreError(RuntimeError):
    """Raised with a learner-facing message for auth storage failures."""


@dataclass(frozen=True, slots=True)
class AuthDbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str = "ref_contest_db"

    @classmethod
    def from_environment(cls) -> "AuthDbConfig":
        password = os.environ.get("REF_AUTH_PASSWORD", "")
        if not password:
            raise AuthStoreError("REF_AUTH_PASSWORD 环境变量缺失，请联系管理员。")
        port_text = os.environ.get("MYSQL_PORT", "3306")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise AuthStoreError("MYSQL_PORT 配置无效，请联系管理员。") from exc
        return cls(
            host=os.environ.get("MYSQL_HOST", "database"),
            port=port,
            user=os.environ.get("REF_AUTH_USER", "ref_auth"),
            password=password,
        )


# ---------------------------------------------------------------- hashing


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 6:
        raise AuthStoreError("密码至少需要 6 位。")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"{PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations_text, salt, expected = stored.split("$", 2)
        iterations = int(iterations_text)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        ).hex()
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest, expected)


def mask_phone(phone: str) -> str:
    if len(phone) == PHONE_RE_DIGITS:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:2] + "****"


def valid_phone(phone: str) -> bool:
    return (
        isinstance(phone, str)
        and len(phone) == PHONE_RE_DIGITS
        and phone.startswith("1")
        and phone.isdigit()
    )


# ---------------------------------------------------------------- store


class AuthStore:
    """CRUD for the users table behind the ref_auth account."""

    def __init__(self, config: AuthDbConfig | None = None) -> None:
        self._config = config or AuthDbConfig.from_environment()

    def _connect(self) -> Any:
        return pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def ensure_schema(self) -> None:
        """Idempotently create the users table (ref_auth owns it)."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                      id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                      phone         VARCHAR(11)  NOT NULL,
                      username      VARCHAR(32)  NOT NULL,
                      password_hash VARCHAR(255) NOT NULL,
                      role          VARCHAR(16)  NOT NULL DEFAULT 'student',
                      created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      UNIQUE KEY uk_phone (phone)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

    def ensure_admin(self, admin_password: str | None) -> bool:
        """Seed the admin account once from the deployment environment.

        Returns True when an admin row exists after the call.  When the
        password is missing the account is still created with a random
        (undocumented) password so the table shape stays consistent.
        """
        if not admin_password or len(admin_password) < 6:
            return False
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
                )
                if cursor.fetchone() is not None:
                    return True
                cursor.execute(
                    "INSERT INTO users (phone, username, password_hash, role)"
                    " VALUES (%s, 'admin', %s, 'admin')",
                    ("0" * 11, hash_password(admin_password)),
                )
                return True

    def phone_registered(self, phone: str) -> bool:
        if not valid_phone(phone):
            raise AuthStoreError("请输入 11 位有效手机号。")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM users WHERE phone = %s LIMIT 1", (phone,)
                )
                return cursor.fetchone() is not None

    def register(
        self, phone: str, username: str, password: str
    ) -> dict[str, Any]:
        if not valid_phone(phone):
            raise AuthStoreError("请输入 11 位有效手机号。")
        if not isinstance(username, str) or not (1 <= len(username.strip()) <= 32):
            raise AuthStoreError("用户名需为 1~32 个字符。")
        if len(password) < 6:
            raise AuthStoreError("密码至少需要 6 位。")
        password_hash = hash_password(password)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO users (phone, username, password_hash, role)"
                        " VALUES (%s, %s, %s, 'student')",
                        (phone, username.strip(), password_hash),
                    )
                    user_id = int(cursor.lastrowid)
        except pymysql.err.IntegrityError:
            raise AuthStoreError("该手机号已注册，请直接登录。") from None
        return {"user_id": user_id, "username": username.strip(), "role": "student"}

    def find_by_phone(self, phone: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, phone, username, password_hash, role"
                    " FROM users WHERE phone = %s LIMIT 1",
                    (phone,),
                )
                return cursor.fetchone()

    def find_admin(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, phone, username, password_hash, role"
                    " FROM users WHERE role = 'admin' AND username = %s LIMIT 1",
                    (username,),
                )
                return cursor.fetchone()

    def find_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, phone, username, role"
                    " FROM users WHERE id = %s LIMIT 1",
                    (user_id,),
                )
                return cursor.fetchone()

    def update_password(self, user_id: int, new_password: str) -> None:
        if len(new_password) < 6:
            raise AuthStoreError("新密码至少需要 6 位。")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (hash_password(new_password), user_id),
                )
                if cursor.rowcount == 0:
                    raise AuthStoreError("账号不存在或已失效，请重新登录。")
