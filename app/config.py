"""全局配置：全部来自环境变量，不依赖任何外部服务。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


#: 交易时段的默认值：与 A 股内置时段表保持一致。这样即使库里没有存过
#: 时段表，服务也按 A 股时段跑，运营不必对着空文本框自己拼 JSON。
DEFAULT_SESSION_SLOTS = (
    '[{"start":"09:15","end":"09:25","label":"开盘集合竞价","segment":"auction","match":false,"trade":true},'
    '{"start":"09:25","end":"09:30","label":"待开盘","segment":"auction","match":false,"trade":true},'
    '{"start":"09:30","end":"11:30","label":"上午连续竞价","segment":"continuous","match":true,"trade":true},'
    '{"start":"11:30","end":"13:00","label":"午间休市","segment":"lunch","match":false,"trade":false},'
    '{"start":"13:00","end":"14:57","label":"下午连续竞价","segment":"continuous","match":true,"trade":true},'
    '{"start":"14:57","end":"15:00","label":"收盘集合竞价","segment":"auction","match":false,"trade":true},'
    '{"start":"15:05","end":"15:30","label":"盘后固定价格交易","segment":"fixed","match":false,"trade":true}]'
)


#: 休市日期的默认值：空数组。周末由代码判定，无需登记；法定节假日由运营
#: 按年维护（后台有一键填入 A 股当年节假日 的按钮）。
DEFAULT_HOLIDAYS = "[]"


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
        self.access_token_ttl: int = int(os.getenv("STLAND_ACCESS_TOKEN_TTL", "7200"))
        self.refresh_token_ttl: int = int(os.getenv("STLAND_REFRESH_TOKEN_TTL", str(60 * 60 * 24 * 14)))
        self.login_fail_limit: int = int(os.getenv("STLAND_LOGIN_FAIL_LIMIT", "8"))
        # 后台会话 Cookie 是否只走 HTTPS。生产（反代 TLS 终结）应设 1；
        # 本地 http 调试默认关闭，否则浏览器不回传 Cookie，后台永远登录不上。
        self.admin_cookie_secure: bool = os.getenv("STLAND_ADMIN_COOKIE_SECURE", "0") == "1"

        # ---- 交易 ----
        self.seed_cash: float = float(os.getenv("STLAND_SEED_CASH", "1000000"))
        self.commission_rate: float = float(os.getenv("STLAND_COMMISSION_RATE", "0.00025"))
        self.commission_min: float = float(os.getenv("STLAND_COMMISSION_MIN", "5.0"))
        self.stamp_tax_rate: float = float(os.getenv("STLAND_STAMP_TAX_RATE", "0.0005"))
        self.transfer_fee_rate: float = float(os.getenv("STLAND_TRANSFER_FEE_RATE", "0.00001"))

        # ---- 委托风控 ----
        # 单笔 / 单日金额上限，按账户类型分档（单位：元，0 表示不限制）。
        # 分档而不是共用一个阈值：机构的下单规模天然比零售大一个量级，
        # 共用阈值要么卡死机构，要么放过零售。
        self.risk_max_order_retail: float = float(os.getenv("STLAND_RISK_MAX_ORDER_RETAIL", "200000"))
        self.risk_daily_limit_retail: float = float(os.getenv("STLAND_RISK_DAILY_LIMIT_RETAIL", "2000000"))
        self.risk_max_order_institution: float = float(os.getenv("STLAND_RISK_MAX_ORDER_INSTITUTION", "5000000"))
        self.risk_daily_limit_institution: float = float(os.getenv("STLAND_RISK_DAILY_LIMIT_INSTITUTION", "50000000"))

        # ---- 撮合 / 模拟引擎 ----
        self.tick_seconds: float = float(os.getenv("STLAND_TICK_SECONDS", "1"))
        # 交易模式：a_share=A股（默认，含集合竞价）/ us=美股 / continuous=24h 连续竞价
        # 旧环境变量 STLAND_SESSION_MODE 仍兼容（always->continuous, real->a_share）
        # 结算模式：t1=当日买入次日可卖 / t0=当日买入当日可卖 / debug=调试（时段失效）
        self.trade_mode: str = os.getenv(
            "STLAND_TRADE_MODE", os.getenv("STLAND_SESSION_MODE", "t1")
        )
        # 交易时段：只存一份 JSON 时段表，默认值即 A 股内置时段
        self.session_slots: str = os.getenv("STLAND_SESSION_SLOTS", DEFAULT_SESSION_SLOTS)
        # 模拟引擎强度系数（运行期可调）
        self.market_volatility: float = float(os.getenv("STLAND_MARKET_VOLATILITY", "1.0"))
        self.market_drift: float = float(os.getenv("STLAND_MARKET_DRIFT", "1.0"))
        # 兼容旧属性名，避免第三方脚本直接读 settings.session_mode 时报错
        self.session_mode: str = self.trade_mode
        self.force_open: bool = _bool("STLAND_FORCE_OPEN", False)
        # 模拟引擎默认关闭：新部署启动后不自动推进行情，等管理员在
        # 「模拟配置」里确认参数再手动打开，避免上线瞬间价格就开始乱跳。
        self.engine_enabled: bool = _bool("STLAND_ENGINE_ENABLED", False)
        # 行情源：simulated=内置模拟引擎 / external=外部真实行情。
        # 与 engate_enabled 是两个独立开关 —— 「行情从哪来」和
        # 「引擎跑不跑」不是一回事，原先焊死在一起才导致关引擎即关市场。
        self.quote_source: str = os.getenv("STLAND_QUOTE_SOURCE", "simulated")
        # 撮合模式：book=只撮合用户订单簿（真实市场，无人接单就挂着）
        #           hybrid=先吃用户订单簿，剩余部分对做市商（原合成盘口）成交
        # 默认 hybrid 是为了让模拟盘保持原有手感；要跑「纯真实交易」应切 book。
        # 默认 book：只撮合真实用户订单簿。
        # 之前默认 hybrid —— 用户单吃不到对手时由做市商兜底成交，等于默认
        # 打开了一台注水机：成交量里有相当部分是和引擎成交的，而非真实对手。
        # 想做演示盘再显式 STLAND_MATCH_MODE=hybrid 打开。
        self.match_mode: str = os.getenv("STLAND_MATCH_MODE", "book")
        # 休市日期：非周末的特殊休市日（法定节假日），JSON 日期数组。
        # 真实交易必须有交易日历 —— 假期里照常撮合、照常收单，模拟的
        # 时间和真实市场就对不上了。
        self.holidays: str = os.getenv("STLAND_HOLIDAYS", DEFAULT_HOLIDAYS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
