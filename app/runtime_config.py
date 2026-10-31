"""运行期配置：后台面板可在线修改、立即生效的那一部分配置。

为什么需要这一层
--------------
`app/config.py` 里的配置全部来自环境变量，适合「部署时决定」的东西
（数据库地址、JWT 密钥、端口）。但有一类参数属于**运营日常调节**：
佣金费率、印花税、新账户初始资金、行情推进速度、是否允许全天连续竞价……
这些改一次就要重启服务、甚至重新下发环境变量，很不合理。

所以把它们抽成「运行期配置」：值存 `setting` 表，进程内直接改写
`settings` 对象的同名属性。业务代码读的仍然是 `settings.commission_rate`，
不需要知道配置来自环境变量还是数据库 —— 改完下一次调用就生效。

安全边界
--------
* 只开放白名单里的键（`RUNTIME_SPECS`），不接受任意键写入，
  避免运营把 `jwt_secret` 改成空串这种事故；
* 每个键都带类型、取值范围与说明，后端直接拒绝越界值；
* 不缓存到进程外，重启后从库里重新加载，环境变量只是「初始默认值」。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from .config import settings
from .models import Setting

logger = logging.getLogger("stland.runtime_config")


@dataclass(frozen=True)
class Spec:
    """一个可调配置项的定义。"""

    key: str
    label: str
    kind: str            # float / int / bool / choice
    default: Any
    minimum: float = 0.0
    maximum: float = 0.0
    choices: tuple[str, ...] = ()
    unit: str = ""
    note: str = ""
    # 选项的展示名（value -> 中文标签）。为空时后台直接显示 value，
    # 交易模式这种「存 a_share、显示 A股」的项必须给标签，否则运营看不懂下拉框
    choice_labels: tuple[tuple[str, str], ...] = ()

    def coerce(self, raw: Any) -> Any:
        """把前端传来的字符串/数字规范成目标类型，越界抛 ValueError。"""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{self.label} 只接受布尔值")
        if self.kind == "choice":
            text = str(raw).strip()
            if text not in self.choices:
                raise ValueError(f"{self.label} 只接受 {'/'.join(self.choices)}")
            return text
        if self.kind == "int":
            val: Any = int(float(str(raw).strip()))
        else:
            val = float(str(raw).strip())
        if self.minimum and val < self.minimum:
            raise ValueError(f"{self.label} 不能小于 {self.minimum:g}{self.unit}")
        if self.maximum and val > self.maximum:
            raise ValueError(f"{self.label} 不能大于 {self.maximum:g}{self.unit}")
        return val

    def to_text(self, value: Any) -> str:
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)

    def to_json(self, value: Any) -> Any:
        if self.kind == "bool":
            return bool(value)
        if self.kind == "int":
            return int(value)
        if self.kind == "choice":
            return str(value)
        return float(value)


# 白名单：键名必须与 `settings` 上的属性同名，改完直接覆盖属性即生效
RUNTIME_SPECS: dict[str, Spec] = {
    "commission_rate": Spec(
        "commission_rate", "佣金费率", "float", 0.00025,
        minimum=0.0, maximum=0.003, note="万2.5 = 0.00025，双向收取",
    ),
    "commission_min": Spec(
        "commission_min", "最低佣金", "float", 5.0,
        minimum=0.0, maximum=100.0, unit="元", note="单笔佣金下限",
    ),
    "stamp_tax_rate": Spec(
        "stamp_tax_rate", "印花税率", "float", 0.0005,
        minimum=0.0, maximum=0.003, note="仅卖出收取，万5 = 0.0005",
    ),
    "transfer_fee_rate": Spec(
        "transfer_fee_rate", "过户费率", "float", 0.00001,
        minimum=0.0, maximum=0.001, note="双向收取，万0.1 = 0.00001",
    ),
    "seed_cash": Spec(
        "seed_cash", "新账户初始资金", "float", 1_000_000.0,
        minimum=0.0, maximum=1e9, unit="元", note="注册与批量建户默认值",
    ),
    "tick_seconds": Spec(
        "tick_seconds", "行情推进间隔", "float", 1.0,
        minimum=0.2, maximum=60.0, unit="秒", note="越小行情跳动越快，CPU 占用也越高",
    ),
    "trade_mode": Spec(
        "trade_mode", "交易模式", "choice", "a_share",
        choices=("a_share", "us", "continuous"),
        choice_labels=(
            ("a_share", "A股"),
            ("us", "美股"),
            ("continuous", "连续竞价"),
        ),
        note="A股=9:30-11:30/13:00-15:00 连续竞价（含开盘/收盘集合竞价）；美股=美东时段（含盘前盘后）；连续竞价=24小时不间断",
    ),
    "market_volatility": Spec(
        "market_volatility", "市场波动率系数", "float", 1.0,
        minimum=0.1, maximum=5.0,
        note="1.0 为基准；调大行情更活跃，调小更平稳（影响模拟引擎随机游走幅度）",
    ),
    "market_drift": Spec(
        "market_drift", "市场漂移系数", "float", 1.0,
        minimum=-3.0, maximum=3.0,
        note=">0 长期偏多，<0 长期偏空；0 表示无系统性方向",
    ),
    "engine_enabled": Spec(
        "engine_enabled", "行情引擎开关", "bool", True, note="关闭后行情冻结，便于排查问题",
    ),
    "signature_required": Spec(
        "signature_required", "请求签名校验", "bool", True,
        note="关闭后不再校验 HMAC 签名，仅建议联调期临时关闭",
    ),
    "history_days": Spec(
        "history_days", "历史日线回填天数", "int", 240,
        minimum=30, maximum=720, unit="天", note="初始化数据时回填多少根日线",
    ),
    "login_fail_limit": Spec(
        "login_fail_limit", "登录失败锁定阈值", "int", 8,
        minimum=3, maximum=50, unit="次", note="连续失败达到该次数后锁定 5 分钟",
    ),
}


def apply_value(key: str, value: Any) -> None:
    """把值写到 `settings` 同名属性上（业务代码下一次调用即生效）。"""
    if key in settings.__dict__:
        setattr(settings, key, value)


def _capture_baseline() -> dict[str, Any]:
    """记录「部署基线」：进程启动那一刻由环境变量决定的取值。

    为什么需要它：`Spec.default` 只是代码里的**兜底常量**，但实际部署常常用
    环境变量覆盖（例如联调期 `STLAND_SIGNATURE_REQUIRED=false`）。
    如果「恢复默认」直接写回 `spec.default`，就会把环境变量的设置也一起抹掉 ——
    典型后果是本来关着的请求签名校验被重新打开，客户端立刻整片 400。
    所以「恢复默认」的正确语义是**回到部署时的取值**，而不是回到代码常量。
    """
    return {key: getattr(settings, key, spec.default) for key, spec in RUNTIME_SPECS.items()}


# 导入即定格：此刻 settings 已读完环境变量，且尚未被数据库覆盖值修改
_BASELINE: dict[str, Any] = _capture_baseline()


def current_value(key: str) -> Any:
    spec = RUNTIME_SPECS[key]
    return getattr(settings, key, _BASELINE.get(key, spec.default))


def default_value(key: str) -> Any:
    """该项的默认值（部署基线优先，其次代码常量）。"""
    return _BASELINE.get(key, RUNTIME_SPECS[key].default)


def snapshot() -> list[dict]:
    """返回全部可调项的当前值 + 元信息，供后台渲染表单。"""
    items: list[dict] = []
    for key, spec in RUNTIME_SPECS.items():
        value = current_value(key)
        items.append(
            {
                "key": key,
                "label": spec.label,
                "kind": spec.kind,
                "value": spec.to_json(value),
                "default": spec.to_json(default_value(key)),
                "min": spec.minimum,
                "max": spec.maximum,
                "choices": list(spec.choices),
                "choice_labels": [{"value": v, "label": t} for v, t in spec.choice_labels],
                "unit": spec.unit,
                "note": spec.note,
            }
        )
    return items


def load_overrides(session: Session) -> int:
    """启动时把库里的覆盖值加载进内存，返回生效条数。"""
    applied = 0
    for row in session.exec(select(Setting)).all():
        spec = RUNTIME_SPECS.get(row.key)
        if spec is None:
            continue          # 已下线的键，忽略但不删除（便于回滚）
        try:
            apply_value(row.key, spec.coerce(row.value))
            applied += 1
        except (ValueError, TypeError) as exc:
            logger.warning("运行期配置 %s 值非法（%s），沿用默认值", row.key, exc)
    return applied


def update(session: Session, changes: dict[str, Any], operator: str = "admin") -> dict:
    """按键更新配置。返回 {updated: [...], rejected: [{key, reason}]}。"""
    updated: list[str] = []
    rejected: list[dict] = []
    for key, raw in changes.items():
        spec = RUNTIME_SPECS.get(key)
        if spec is None:
            rejected.append({"key": key, "reason": "不支持修改该配置项"})
            continue
        try:
            value = spec.coerce(raw)
        except (ValueError, TypeError) as exc:
            rejected.append({"key": key, "reason": str(exc)})
            continue
        apply_value(key, value)
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=spec.to_text(value), updated_by=operator)
        else:
            row.value = spec.to_text(value)
            row.updated_by = operator
            import time as _t

            row.updated_at = _t.time()
        session.add(row)
        updated.append(key)
    session.commit()
    return {"updated": updated, "rejected": rejected}


def reset_defaults(session: Session, operator: str = "admin") -> dict:
    """把全部可调项恢复为默认值，并清空库里的覆盖记录。

    注意用 `default_value` 而非 `spec.default`：默认值必须是**部署基线**
    （环境变量决定的取值），否则会把联调期关掉的开关重新打开。
    """
    for key, spec in RUNTIME_SPECS.items():
        apply_value(key, default_value(key))
        row = session.get(Setting, key)
        if row is not None:
            session.delete(row)
    session.commit()
    return {"ok": True, "count": len(RUNTIME_SPECS)}
