"""安全层：口令散列、JWT 签发/校验、请求签名（HMAC-SHA256）与重放防护。

设计要点（生产可用）：
* 口令使用 PBKDF2-HMAC-SHA256 + 每用户随机盐 + 高迭代次数，绝不明文存储；
* 访问令牌为 HS256 JWT，含 jti / iat / exp / role，服务端支持即时吊销；
* 每个业务请求携带 ``X-Stland-Sign = HMAC-SHA256(secret, canonical)``，
  canonical 由「方法 / 路径 / 时间戳 / 随机数 / 请求体摘要 / 用户标识」按行拼接，
  防止抓包篡改与重放；时间窗口内的随机数只允许使用一次。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from .config import settings

PBKDF2_ROUNDS = 210_000
NEWLINE = chr(10)


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: str | None = None) -> str:
    """返回 pbkdf2_sha256$轮数$盐$摘要 形式的可存储字符串。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return "pbkdf2_sha256" + "$" + str(PBKDF2_ROUNDS) + "$" + salt + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较，抵御计时侧信道。"""
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)
        )
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT（HS256，零第三方依赖实现）
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_token(payload: dict[str, Any], ttl: int, secret: str | None = None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body: dict[str, Any] = {
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(12),
        "iss": "stland-server",
    }
    body.update(payload)
    head = _b64e(json.dumps(header, separators=(",", ":")).encode())
    data = _b64e(json.dumps(body, separators=(",", ":")).encode())
    signing_input = (head + "." + data).encode("ascii")
    sig = hmac.new((secret or settings.jwt_secret).encode(), signing_input, hashlib.sha256).digest()
    return head + "." + data + "." + _b64e(sig)


class TokenError(Exception):
    """令牌无效或已过期。"""


def decode_token(token: str, secret: str | None = None) -> dict[str, Any]:
    try:
        head, data, sig = token.split(".")
    except ValueError as exc:
        raise TokenError("令牌格式错误") from exc
    signing_input = (head + "." + data).encode("ascii")
    expected = hmac.new(
        (secret or settings.jwt_secret).encode(), signing_input, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64d(sig), expected):
        raise TokenError("令牌签名校验失败")
    try:
        payload = json.loads(_b64d(data))
    except (ValueError, TypeError) as exc:
        raise TokenError("令牌载荷解析失败") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("令牌已过期")
    if payload.get("iss") != "stland-server":
        raise TokenError("令牌签发方不合法")
    return payload


# --------------------------------------------------------------------------- #
# 请求签名
# --------------------------------------------------------------------------- #
class NonceCache:
    """内存窗口内 nonce 去重；多实例部署时可替换为 Redis 实现。"""

    def __init__(self, window: int = 300) -> None:
        self.window = window
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str) -> bool:
        now = time.time()
        expired = [k for k, expire in self._seen.items() if expire < now]
        for key in expired:
            self._seen.pop(key, None)
        if not nonce or len(nonce) > 128 or nonce in self._seen:
            return False
        self._seen[nonce] = now + self.window
        return True


nonce_cache = NonceCache(window=max(60, settings.clock_skew * 5))


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def canonical_string(
    method: str, path: str, ts: str, nonce: str, body_sha256: str, user_id: str = ""
) -> str:
    return NEWLINE.join([method.upper(), path, ts, nonce, body_sha256, user_id])


def compute_signature(
    method: str, path: str, ts: str, nonce: str, body_sha256: str, user_id: str = ""
) -> str:
    canonical = canonical_string(method, path, ts, nonce, body_sha256, user_id)
    return hmac.new(settings.sign_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify_signature(
    *,
    method: str,
    path: str,
    ts: str,
    nonce: str,
    body_sha256: str,
    signature: str,
    user_id: str = "",
) -> tuple[bool, str]:
    """返回 (是否通过, 失败原因)。"""
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False, "时间戳格式错误"
    if abs(time.time() - ts_int) > settings.clock_skew:
        return False, "时间戳超出允许窗口"
    expected = compute_signature(method, path, ts, nonce, body_sha256, user_id)
    if not hmac.compare_digest(expected, signature or ""):
        return False, "请求签名校验失败"
    if not nonce_cache.check_and_add(nonce):
        return False, "重复的请求随机数（疑似重放）"
    return True, ""


def random_secret(n: int = 32) -> str:
    return os.urandom(n).hex()
