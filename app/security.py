"""安全层：口令散列、JWT 签发/校验、JWT 签发/校验。

设计要点（生产可用）：
* 口令使用 PBKDF2-HMAC-SHA256 + 每用户随机盐 + 高迭代次数，绝不明文存储；
* 访问令牌为 HS256 JWT，含 jti / iat / exp / role，服务端支持即时吊销；

JWT 交给 PyJWT：算法轮换、kid、clock skew、alg=none 等边界由其处理并测试，
自研编解码只会带来长期维护负担。token 为 HS256 + 标准 claims。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import jwt

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
# JWT（HS256，基于 PyJWT）
# --------------------------------------------------------------------------- #
# 令牌的对外约定保持不变，所以下面两个函数仍是全项目唯一的签发/校验入口：
#   * 算法固定 HS256，且校验时显式指定 algorithms=["HS256"]，
#     这样伪造 alg=none 或换成 RS256 的令牌都会被直接拒绝；
#   * 只带 iss 校验（签发方），不校验 aud —— 本服务没有多受众场景。
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "stland-server"


def create_token(payload: dict[str, Any], ttl: int, secret: str | None = None) -> str:
    """签发访问令牌：自动补 iat / exp / jti / iss，其余 claim 由调用方给。"""
    now = int(time.time())
    body: dict[str, Any] = {
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(12),
        "iss": JWT_ISSUER,
    }
    body.update(payload)
    return jwt.encode(body, secret or settings.jwt_secret, algorithm=JWT_ALGORITHM)


class TokenError(Exception):
    """令牌无效或已过期。"""


def decode_token(token: str, secret: str | None = None) -> dict[str, Any]:
    """校验并解出载荷。

    把 PyJWT 的各类异常统一收敛成本模块的 ``TokenError``：
    上层只关心「令牌能不能用、为什么不能用」，不该被迫认识
    ExpiredSignatureError / InvalidAlgorithmError 等一串异常类型。
    """
    try:
        # 显式限定算法是必须的：不写的话 PyJWT 会按 header 里的 alg 走，
        # 等于把「用什么算法验签」的决定权交给攻击者伪造的令牌头。
        return jwt.decode(
            token,
            secret or settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenError("令牌签发方不合法") from exc
    except jwt.InvalidTokenError as exc:
        # 覆盖格式错误、签名不通过、算法不符、缺必需 claim 等情况
        raise TokenError("令牌无效") from exc


# --------------------------------------------------------------------------- #
# 请求签名
# --------------------------------------------------------------------------- #







def random_secret(n: int = 32) -> str:
    return os.urandom(n).hex()
