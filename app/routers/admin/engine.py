"""管理端 · 模拟引擎（后台「模拟引擎」页签）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 模拟引擎（后台「模拟引擎」页签）
# --------------------------------------------------------------------------- #
class EngineControlIn(BaseModel):
    """引擎开关请求体。"""

    enabled: bool


@router.get("/reconcile")
def reconcile_report(
    session: Session = Depends(get_session), admin: Admin = Depends(admin_user)
) -> dict:
    """账务对账：把订单/成交/持仓/资金相互校验。

    只读接口，不改任何数据 —— 对账的第一步是「看清楚哪里不对」，
    自动修复必须由人确认后再做，否则会把真实差异抹平成看似一致的假账。
    """
    return reconcile.run_all(session)


@router.get("/engine")
def engine_status(admin: Admin = Depends(admin_user)) -> dict:
    """引擎运行态：开关、推进节奏、交易模式、当前时段、标的统计。

    与「系统配置」页签的分工：那边改的是**参数**（费率、tick 间隔等），
    这里给的是**运行态**，并额外提供启停与重置两个动作，方便运营看到
    「现在是 A 股模式、处于午间休市、价格没有推进」这类因果。
    """
    from ...engine.session import TRADE_MODES

    status = market_engine.status()
    status["modes"] = [{"value": k, "label": v} for k, v in TRADE_MODES.items()]
    return status


@router.post("/engine/toggle")
def engine_toggle(
    payload: EngineControlIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """启停行情引擎。关闭后价格冻结，但账户、持仓与撮合数据都不动。"""
    result = runtime_config.update(
        session, {"engine_enabled": payload.enabled}, operator=admin.username
    )
    if result["rejected"]:
        raise HTTPException(400, detail=result["rejected"][0])
    status = market_engine.status()
    status["modes"] = []
    return status


@router.post("/engine/reset")
def engine_reset(admin: Admin = Depends(admin_user)) -> dict:
    """重置内存行情：所有标的回到昨收，清空分钟线与累计成交。

    只影响行情快照，不动标的参数、账户资金与持仓，因此不会破坏对账。
    """
    status = market_engine.reset()
    status["modes"] = []
    return status
