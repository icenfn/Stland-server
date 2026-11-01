"""委托风控测试。

钉住三件事：
1. **按账户类型分档**：同一个金额零售被拒、机构放行 —— 共用一个阈值
   要么卡死机构，要么放过零售；
2. **单笔上限**在冻结之前拦截，不留冻结/解冻的废流水；
3. **单日累计**把在途委托算进去，否则同一笔钱能在限额内反复挂单。
"""
from __future__ import annotations

import os
import tempfile
import time

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import Order, OrderSide, OrderStatus, OrderType, Role, Trade, User  # noqa: E402
from app.services import risk  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _user(db: Session, role: Role, cash: float = 1e9) -> User:
    u = User(username=f"u{role.value}{time.time()}", role=role, cash=cash)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def limits(monkeypatch):
    """把额度拧成好算的数：零售单笔 20 万 / 单日 40 万；机构单笔 500 万 / 单日 1000 万。"""
    monkeypatch.setattr(settings, "risk_max_order_retail", 200_000.0)
    monkeypatch.setattr(settings, "risk_daily_limit_retail", 400_000.0)
    monkeypatch.setattr(settings, "risk_max_order_institution", 5_000_000.0)
    monkeypatch.setattr(settings, "risk_daily_limit_institution", 10_000_000.0)


def test_limits_differ_by_role(limits):
    assert risk.limits_for(Role.RETAIL) == (200_000.0, 400_000.0)
    assert risk.limits_for(Role.INSTITUTION) == (5_000_000.0, 10_000_000.0)


def test_zero_limit_means_unlimited(monkeypatch):
    monkeypatch.setattr(settings, "risk_max_order_retail", 0.0)
    monkeypatch.setattr(settings, "risk_daily_limit_retail", 0.0)
    assert risk.limits_for(Role.RETAIL) == (0.0, 0.0)


def test_retail_single_order_cap(db, limits):
    u = _user(db, Role.RETAIL)
    with pytest.raises(risk.RiskError) as err:
        risk.check_order(db, u, price=100.0, quantity=3000)  # 30 万
    assert err.value.code == "ORDER_LIMIT_EXCEEDED"


def test_same_amount_passes_for_institution(db, limits):
    """同一笔 30 万：零售被拒，机构放行 —— 分档确实生效。"""
    u = _user(db, Role.INSTITUTION)
    risk.check_order(db, u, price=100.0, quantity=3000)


def test_within_cap_passes(db, limits):
    u = _user(db, Role.RETAIL)
    risk.check_order(db, u, price=100.0, quantity=1000)  # 10 万


def test_daily_limit_counts_filled_trades(db, limits):
    u = _user(db, Role.RETAIL)
    db.add(Trade(trade_no="T1", order_id=1, user_id=u.id, code="X", side=OrderSide.BUY,
                 price=100.0, quantity=3000, amount=300_000.0, created_at=time.time()))
    db.commit()
    # 已用 30 万，再来 15 万 → 45 万，超过 40 万上限
    with pytest.raises(risk.RiskError) as err:
        risk.check_order(db, u, price=100.0, quantity=1500)
    assert err.value.code == "DAILY_LIMIT_EXCEEDED"


def test_daily_limit_counts_resting_orders(db, limits):
    """在途委托也要占额度，否则同一笔钱能在限额内反复挂单。"""
    u = _user(db, Role.RETAIL)
    db.add(Order(order_no="O1", user_id=u.id, code="X", side=OrderSide.BUY,
                 order_type=OrderType.LIMIT, price=100.0, quantity=3000,
                 status=OrderStatus.PENDING, created_at=time.time()))
    db.commit()
    with pytest.raises(risk.RiskError):
        risk.check_order(db, u, price=100.0, quantity=1500)


def test_resting_only_counts_unfilled_part(db, limits):
    """部分成交的在途委托只按未成交余量占额度，已成交部分不重复计。"""
    u = _user(db, Role.RETAIL)
    db.add(Order(order_no="O2", user_id=u.id, code="X", side=OrderSide.BUY,
                 order_type=OrderType.LIMIT, price=100.0, quantity=3000,
                 filled_quantity=2900, status=OrderStatus.PARTIAL, created_at=time.time()))
    db.commit()
    # 在途只占 100 股 = 1 万，再加 15 万仍在 40 万内
    risk.check_order(db, u, price=100.0, quantity=1500)


def test_institution_uses_its_own_daily_limit(db, limits):
    u = _user(db, Role.INSTITUTION)
    risk.check_order(db, u, price=100.0, quantity=30_000)  # 300 万，远超零售单日上限
