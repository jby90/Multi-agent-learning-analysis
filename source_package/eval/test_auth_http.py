"""Unit tests for the login/registration module (no real database).

The AuthStore is exercised against a fully scripted fake connection, and the
HTTP handler is driven end-to-end for register/login/admin/change-password/me,
including the unified wrong-credentials message and token tampering.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from orchestrator.auth_http import (
    AuthHttpError,
    AuthHttpHandler,
    issue_token,
    read_token,
)
from orchestrator import auth_store as store_mod
from orchestrator.auth_store import (
    AuthStore,
    AuthStoreError,
    hash_password,
    mask_phone,
    valid_phone,
    verify_password,
)


# --------------------------------------------------------------- fakes


class FakeCursor:
    def __init__(self, store: "FakeConnection") -> None:
        self._store = store
        self.lastrowid = 0
        self.rowcount = 0
        self._result: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._result = self._store.execute(sql, params)
        if sql.lstrip().upper().startswith("INSERT"):
            self._store.next_id += 1
            self.lastrowid = self._store.next_id
            self.rowcount = 1
        elif sql.lstrip().upper().startswith("UPDATE"):
            # UPDATE 语句 execute() 不返回行集；用 self._store 执行结果判定。
            matched = self._store.execute(sql, params)
            self.rowcount = 1 if matched is not None else 0

    def fetchone(self) -> dict[str, Any] | None:
        return dict(self._result[0]) if self._result else None


class FakeConnection:
    def __init__(self) -> None:
        self.next_id = 0
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("SELECT 1 FROM users WHERE phone"):
            return [r for r in self.rows if r["phone"] == params[0]]
        if sql_upper.startswith("SELECT") and "WHERE phone" in sql:
            return [r for r in self.rows if r["phone"] == params[0]]
        if sql_upper.startswith("SELECT") and "role = 'admin'" in sql:
            return [
                r for r in self.rows
                if r["role"] == "admin" and r["username"] == params[0]
            ]
        if sql_upper.startswith("SELECT") and "WHERE id" in sql:
            return [r for r in self.rows if r["id"] == params[0]]
        if sql_upper.startswith("SELECT 1 FROM users WHERE role"):
            return [r for r in self.rows if r["role"] == "admin"][:1]
        if sql_upper.startswith("INSERT"):
            for row in self.rows:
                if row["phone"] == params[0]:
                    import pymysql

                    raise pymysql.err.IntegrityError(1062, "duplicate phone")
            self.rows.append(
                {
                    "id": self.next_id + 1,
                    "phone": params[0],
                    "username": params[1],
                    "password_hash": params[2],
                    "role": params[3] if len(params) > 3 else "student",
                }
            )
            return []
        if sql_upper.startswith("UPDATE"):
            for row in self.rows:
                if row["id"] == params[1]:
                    row["password_hash"] = params[0]
                    return "matched"
            return None
        raise AssertionError(f"unexpected SQL in fake: {sql[:80]}")

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def make_store(monkeypatch: pytest.MonkeyPatch) -> tuple[AuthStore, FakeConnection]:
    fake = FakeConnection()
    monkeypatch.setattr(AuthStore, "_connect", lambda self: fake)
    return AuthStore.__new__(AuthStore), fake


# --------------------------------------------------------------- hashing


def test_hash_and_verify_roundtrip() -> None:
    stored = hash_password("hunter2secret")
    assert "$" in stored
    assert verify_password("hunter2secret", stored)
    assert not verify_password("wrong-password", stored)


def test_hash_rejects_short_passwords() -> None:
    with pytest.raises(AuthStoreError):
        hash_password("12345")


def test_hash_salt_uniqueness() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_phone_helpers() -> None:
    assert valid_phone("13812345678")
    assert not valid_phone("1381234567")
    assert not valid_phone("23812345678")
    assert mask_phone("13812345678") == "138****5678"


# --------------------------------------------------------------- token


def test_token_roundtrip_and_expiry() -> None:
    token = issue_token(7, "student")
    claims = read_token(token)
    assert claims["user_id"] == 7
    assert claims["role"] == "student"
    assert claims["exp"] > time.time()


def test_token_tampering_rejected() -> None:
    token = issue_token(7, "student")
    with pytest.raises(AuthHttpError):
        read_token(token[:-2] + "zz")


def test_token_expired_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    expired = issue_token(7, "student")
    # 未过期时正常读取
    assert read_token(expired)["user_id"] == 7
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 25 * 3600)
    with pytest.raises(AuthHttpError):
        read_token(expired)


# --------------------------------------------------------------- handler


def make_handler(fake: FakeConnection) -> AuthHttpHandler:
    store = AuthStore.__new__(AuthStore)
    store._connect = lambda: fake  # type: ignore[attr-defined]
    return AuthHttpHandler(store)


def test_register_login_me_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    status, payload = handler.handle(
        "POST", "/api/auth/register", "",
        json.dumps({"phone": "13812345678", "username": "小陈", "password": "abc123", "confirm": "abc123"}).encode(),
    )
    assert status == 201
    assert payload["user_id"] == 1
    assert payload["role"] == "student"
    assert payload["token"]

    status, me = handler.handle(
        "GET", "/api/auth/me", f"token={payload['token']}", b""
    )
    assert status == 200
    assert me["username"] == "小陈"
    assert me["phone_masked"] == "138****5678"

    status, login = handler.handle(
        "POST", "/api/auth/login", "",
        json.dumps({"phone": "13812345678", "password": "abc123"}).encode(),
    )
    assert status == 200 and login["token"]


def test_register_duplicate_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    body = json.dumps({"phone": "13812345678", "username": "a", "password": "abc123"}).encode()
    handler.handle("POST", "/api/auth/register", "", body)
    with pytest.raises(AuthHttpError) as raised:
        handler.handle("POST", "/api/auth/register", "", body)
    assert "已注册" in str(raised.value)


def test_check_phone_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    _, result = handler.handle("GET", "/api/auth/check-phone", "phone=13812345678", b"")
    assert result == {"registered": False}
    handler.handle(
        "POST", "/api/auth/register", "",
        json.dumps({"phone": "13812345678", "username": "a", "password": "abc123"}).encode(),
    )
    _, result = handler.handle("GET", "/api/auth/check-phone", "phone=13812345678", b"")
    assert result == {"registered": True}
    with pytest.raises(AuthHttpError):
        handler.handle("GET", "/api/auth/check-phone", "phone=123", b"")


def test_login_wrong_credentials_unified_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    handler.handle(
        "POST", "/api/auth/register", "",
        json.dumps({"phone": "13812345678", "username": "a", "password": "abc123"}).encode(),
    )
    # wrong password
    with pytest.raises(AuthHttpError) as wrong_password:
        handler.handle(
            "POST", "/api/auth/login", "",
            json.dumps({"phone": "13812345678", "password": "nope-nope"}).encode(),
        )
    assert wrong_password.value.message == "手机号或密码错误。"
    # unknown phone — same message (no account enumeration)
    with pytest.raises(AuthHttpError) as unknown:
        handler.handle(
            "POST", "/api/auth/login", "",
            json.dumps({"phone": "13900000000", "password": "abc123"}).encode(),
        )
    assert unknown.value.message == "手机号或密码错误。"


def test_admin_login_and_me_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    fake.rows.append(
        {
            "id": 2,
            "phone": "00000000000",
            "username": "admin",
            "password_hash": hash_password("admin123456"),
            "role": "admin",
        }
    )
    handler = make_handler(fake)
    handler.sessions_online = lambda: 3
    status, payload = handler.handle(
        "POST", "/api/auth/admin-login", "",
        json.dumps({"username": "admin", "password": "admin123456"}).encode(),
    )
    assert status == 200 and payload["role"] == "admin"
    _, me = handler.handle("GET", "/api/auth/me", f"token={payload['token']}", b"")
    assert me["active_sessions"] == 3


def test_admin_cannot_change_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    admin_token = issue_token(2, "admin")
    with pytest.raises(AuthHttpError) as raised:
        handler.handle(
            "POST", "/api/auth/change-password", "",
            json.dumps({"token": admin_token, "old_password": "x", "new_password": "newpass6", "confirm": "newpass6"}).encode(),
        )
    assert raised.value.status == 403


def test_change_password_requires_old(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    _, registered = handler.handle(
        "POST", "/api/auth/register", "",
        json.dumps({"phone": "13812345678", "username": "a", "password": "abc123"}).encode(),
    )
    with pytest.raises(AuthHttpError) as wrong_old:
        handler.handle(
            "POST", "/api/auth/change-password", "",
            json.dumps({"token": registered["token"], "old_password": "badbad", "new_password": "newpass6", "confirm": "newpass6"}).encode(),
        )
    assert wrong_old.value.message == "旧密码不正确。"
    _, done = handler.handle(
        "POST", "/api/auth/change-password", "",
        json.dumps({"token": registered["token"], "old_password": "abc123", "new_password": "newpass6", "confirm": "newpass6"}).encode(),
    )
    assert done == {"ok": True}
    # new password now logs in
    _, login = handler.handle(
        "POST", "/api/auth/login", "",
        json.dumps({"phone": "13812345678", "password": "newpass6"}).encode(),
    )
    assert login["token"]


def test_change_password_rejects_same_as_old(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    _, registered = handler.handle(
        "POST", "/api/auth/register", "",
        json.dumps({"phone": "13812345678", "username": "a", "password": "abc123"}).encode(),
    )
    with pytest.raises(AuthHttpError) as same:
        handler.handle(
            "POST", "/api/auth/change-password", "",
            json.dumps({"token": registered["token"], "old_password": "abc123", "new_password": "abc123", "confirm": "abc123"}).encode(),
        )
    assert same.value.status == 400
    assert same.value.message == "新密码不能与旧密码相同。"
    # 密码未被覆盖，旧密码仍可登录（防止"假更新"）
    _, login = handler.handle(
        "POST", "/api/auth/login", "",
        json.dumps({"phone": "13812345678", "password": "abc123"}).encode(),
    )
    assert login["token"]


def test_register_password_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    with pytest.raises(AuthHttpError) as raised:
        handler.handle(
            "POST", "/api/auth/register", "",
            json.dumps({"phone": "13812345678", "username": "a", "password": "abc123", "confirm": "xyz789"}).encode(),
        )
    assert "不一致" in raised.value.message


def test_unknown_auth_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = make_store(monkeypatch)
    handler = make_handler(fake)
    with pytest.raises(AuthHttpError) as raised:
        handler.handle("GET", "/api/auth/nope", "", b"")
    assert raised.value.status == 404
