"""HTTP handlers for registration, login and account self-service.

Six routes live here (all under ``/api/auth``), implemented on the same
standard-library HTTP server as the training session API.  Tokens are
stateless HMAC-signed payloads so backend restarts do not log learners out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable, Mapping

from orchestrator.auth_store import (
    AuthStore,
    AuthStoreError,
    mask_phone,
    verify_password,
)

TOKEN_TTL_SECONDS = 24 * 60 * 60
_WRONG_CREDENTIALS = "手机号或密码错误。"


class AuthHttpError(RuntimeError):
    """Carries an HTTP status plus a learner-facing message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _auth_secret() -> bytes:
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        # Deterministic fallback keeps local demos working; deployments that
        # care set AUTH_SECRET in .env (documented in .env.example).
        secret = "ref-contest-local-auth-secret"
    return secret.encode("utf-8")


# ---------------------------------------------------------------- token


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(user_id: int, role: str) -> str:
    payload = json.dumps(
        {"user_id": user_id, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(_auth_secret(), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def read_token(token: str) -> dict[str, Any]:
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = _b64decode(payload_text)
        signature = _b64decode(signature_text)
    except (ValueError, TypeError):
        raise AuthHttpError(401, "登录状态无效，请重新登录。") from None
    expected = hmac.new(_auth_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise AuthHttpError(401, "登录状态无效，请重新登录。")
    try:
        claims = json.loads(payload)
    except json.JSONDecodeError:
        raise AuthHttpError(401, "登录状态无效，请重新登录。") from None
    if not isinstance(claims, Mapping) or int(claims.get("exp") or 0) < time.time():
        raise AuthHttpError(401, "登录已过期，请重新登录。")
    return dict(claims)


# ---------------------------------------------------------------- handlers


def _read_json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuthHttpError(400, "请求数据格式不正确。") from None
    if not isinstance(value, dict):
        raise AuthHttpError(400, "请求数据格式不正确。")
    return value


class AuthHttpHandler:
    """Route dispatcher for /api/auth/* requests."""

    def __init__(self, store: AuthStore | None = None) -> None:
        self._store = store or AuthStore()
        self.sessions_online: Callable[[], int] | None = None

    def account_from_token(self, token: str) -> dict[str, Any] | None:
        """Resolve an auth token into {user_id, username, role}; None when invalid.

        无效/过期 token 返回 None（调用方按游客处理），不抛出——用于训练会话
        创建时的学习记录账号归属（0818 需求 5/6）。
        """

        try:
            claims = read_token(token)
        except AuthHttpError:
            return None
        user_id = claims.get("user_id")
        if user_id is None:
            return None
        username = ""
        try:
            row = self._store.find_by_id(int(user_id))
            if row:
                username = str(row.get("username", ""))
        except Exception:
            username = ""
        return {
            "user_id": user_id,
            "username": username,
            "role": str(claims.get("role", "student")),
        }

    # each handler returns (status, payload dict)

    def handle(self, method: str, path: str, query: str, body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            if path == "/api/auth/check-phone" and method == "GET":
                return self._check_phone(query)
            if path == "/api/auth/register" and method == "POST":
                return 201, self._register(body)
            if path == "/api/auth/login" and method == "POST":
                return 200, self._login(body)
            if path == "/api/auth/admin-login" and method == "POST":
                return 200, self._admin_login(body)
            if path == "/api/auth/change-password" and method == "POST":
                return 200, self._change_password(body)
            if path == "/api/auth/me" and method == "GET":
                return 200, self._me(query)
        except AuthStoreError as exc:
            raise AuthHttpError(400, str(exc)) from None
        raise AuthHttpError(404, "未知接口。")

    def _check_phone(self, query: str) -> tuple[int, dict[str, Any]]:
        from urllib.parse import parse_qs

        values = parse_qs(query).get("phone", [])
        phone = values[0] if values else ""
        registered = self._store.phone_registered(phone)
        return 200, {"registered": registered}

    def _register(self, body: bytes) -> dict[str, Any]:
        data = _read_json(body)
        phone = str(data.get("phone") or "")
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        confirm = str(data.get("confirm") or password)
        if password != confirm:
            raise AuthHttpError(400, "两次输入的密码不一致。")
        user = self._store.register(phone, username, password)
        user["token"] = issue_token(user["user_id"], user["role"])
        return user

    def _login(self, body: bytes) -> dict[str, Any]:
        data = _read_json(body)
        phone = str(data.get("phone") or "")
        password = str(data.get("password") or "")
        if not phone or not password:
            raise AuthHttpError(400, _WRONG_CREDENTIALS)
        record = self._store.find_by_phone(phone)
        if record is None or not verify_password(password, str(record["password_hash"])):
            raise AuthHttpError(401, _WRONG_CREDENTIALS)
        return {
            "user_id": int(record["id"]),
            "username": str(record["username"]),
            "role": str(record["role"]),
            "token": issue_token(int(record["id"]), str(record["role"])),
        }

    def _admin_login(self, body: bytes) -> dict[str, Any]:
        data = _read_json(body)
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        if not username or not password:
            raise AuthHttpError(400, "用户名或密码错误。")
        record = self._store.find_admin(username)
        if record is None or not verify_password(password, str(record["password_hash"])):
            raise AuthHttpError(401, "用户名或密码错误。")
        return {
            "user_id": int(record["id"]),
            "username": str(record["username"]),
            "role": "admin",
            "token": issue_token(int(record["id"]), "admin"),
        }

    def _change_password(self, body: bytes) -> dict[str, Any]:
        data = _read_json(body)
        claims = read_token(str(data.get("token") or ""))
        if claims.get("role") == "admin":
            raise AuthHttpError(403, "管理员密码由部署配置管理。")
        record = self._store.find_by_id(int(claims["user_id"]))
        if record is None:
            raise AuthHttpError(401, "账号不存在或已失效，请重新登录。")
        old_password = str(data.get("old_password") or "")
        full = self._store.find_by_phone(str(record["phone"]))
        if full is None or not verify_password(
            old_password, str(full["password_hash"])
        ):
            raise AuthHttpError(400, "旧密码不正确。")
        new_password = str(data.get("new_password") or "")
        confirm = str(data.get("confirm") or new_password)
        if new_password != confirm:
            raise AuthHttpError(400, "两次输入的新密码不一致。")
        if new_password == old_password:
            raise AuthHttpError(400, "新密码不能与旧密码相同。")
        self._store.update_password(int(claims["user_id"]), new_password)
        return {"ok": True}

    def _me(self, query: str) -> dict[str, Any]:
        from urllib.parse import parse_qs

        tokens = parse_qs(query).get("token", [])
        if not tokens:
            raise AuthHttpError(401, "登录状态无效，请重新登录。")
        claims = read_token(tokens[0])
        record = self._store.find_by_id(int(claims["user_id"]))
        if record is None:
            raise AuthHttpError(401, "账号不存在或已失效。")
        payload: dict[str, Any] = {
            "user_id": int(record["id"]),
            "username": str(record["username"]),
            "phone_masked": mask_phone(str(record["phone"])),
            "role": str(record["role"]),
        }
        if record["role"] == "admin" and self.sessions_online is not None:
            payload["active_sessions"] = self.sessions_online()
        return payload

