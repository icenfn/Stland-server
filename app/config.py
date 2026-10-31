"""全局配置：全部来自环境变量，不依赖任何外部服务。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """运行期配置（不可变语义，启动时一次性读取）。"""

    def __init__(self) -> None:
        self.app_name: str = "Stland"
        self.app_version: str = "1.0.0"
        self.host: str = os.getenv("STLAND_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("STLAND_PORT", "8999"))

        root = Path(__file__).resolve().parent.parent
        self.data_dir: Path = Path(os.getenv("STLAND_DATA_DIR", root / "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_url: str = os.getenv(
            "STLAND_DB_URL", f"sqlite:///{self.data_dir / 'stland.db'}"
        )

        # ---- 安全 ----
        self.jwt_secret: str = os.getenv("STLAND_JWT_SECRET", "dev-jwt-secret-stland")
        self.sign_secret: str = os.getenv("STLAND_SIGN_SECRET", "dev-sign-secret-stland")
        self.access_token_ttl: int = int(os.getenv("STLAND_ACCESS_TOKEN_TTL", "7200"))
        self.refresh_token_ttl: int = int(os.getenv("STLAND_REFRESH_TOKEN_TTL", str(60 * 60 * 24 * 14)))
        self.clock_skew: int = int(os.getenv("STLAND_CLOCK_SKEW", "60"))
        self.signature_required: bool = _bool("STLAND_SIGNATURE_REQUIRED", True)
        self.login_fail_limit: int = int(os.getenv("STLAND_LOGIN_FAIL_LIMIT", "8"))

        # ---- 交易 ----
        self.seed_cash: float = float(os.getenv("STLAND_SEED_CASH", "1000000"))
        self.commission_rate: float = float(os.getenv("STLAND_COMMISSION_RATE", "0.00025"))
        self.commission_min: float = float(os.getenv("STLAND_COMMISSION_MIN", "5.0"))
        self.stamp_tax_rate: float = float(os.getenv("STLAND_STAMP_TAX_RATE", "0.0005"))
        self.transfer_fee_rate: float = float(os.getenv("STLAND_TRANSFER_FEE_RATE", "0.00001"))

        # ---- 撮合 / 模拟引擎 ----
        self.tick_seconds: float = float(os.getenv("STLAND_TICK_SECONDS", "1"))
        # 交易模式：a_share=A股（默认，含集合竞价）/ us=美股 / continuous=24h 连续竞价
        # 旧环境变量 STLAND_SESSION_MODE 仍兼容（always->continuous, real->a_share）
        self.trade_mode: str = os.getenv(
            "STLAND_TRADE_MODE", os.getenv("STLAND_SESSION_MODE", "a_share")
        )
        # 兼容旧属性名，避免第三方脚本直接读 settings.session_mode 时报错
        self.session_mode: str = self.trade_mode
        self.force_open: bool = _bool("STLAND_FORCE_OPEN", False)
        self.engine_enabled: bool = _bool("STLAND_ENGINE_ENABLED", True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
