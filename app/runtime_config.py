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

两个域：系统 / 模拟器
------------------
配置项按「它约束的是谁」分成两个域，而不再只看原来那张 page：

* ``system``（系统配置）—— 约束**服务本身**的行为：请求签名校验、登录失败锁定。
  这些跟股市没关系，换成任何业务都成立，属于部署与安全边界。
* ``sim``（模拟器配置）—— 约束**这个模拟股市**怎么跑：行情引擎参数、交易时段、
  结算规则、交易费用、新账户初始资金。它们共同定义「这个模拟盘的市场规则长什么样」。

为什么必须拆开（而不是继续混在一页）：
    原先的 page 只分 ``system`` / ``engine``，但 ``trade_mode``、``session_*``、
    ``seed_cash``、四类费率全在 ``system`` 页。结果是「调佣金」和「改请求签名校验」
    挤在同一张表单里 —— 前者是模拟盘的业务规则、随时要按活动调，
    后者是安全开关、改错就让整个客户端 400。放一起既容易改错对象，
    也让「模拟器配置」这件事没有任何地方可以整体查看/整体恢复默认。
    拆开之后，运营看「模拟器配置」这一页就等于看到这个模拟盘的全部规则。

安全边界
--------
* 只开放白名单里的键（`RUNTIME_SPECS`），不接受任意键写入，
  避免运营把 `jwt_secret` 改成空串这种事故；
* 每个键都带类型、取值范围与说明，后端直接拒绝越界值；
* 不缓存到进程外，重启后从库里重新加载，环境变量只是「初始默认值」。

关于数据迁移
----------
键名一个都没改，只是新增了「归属哪个域」这个元数据。
`setting` 表里已有的覆盖值按 key 直接命中，改完之后自动落到新分组下，
不需要写迁移脚本 —— 这也是刻意不改键名的原因：改键名等于把所有已调过的
费率、模式统统打回默认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from .config import settings
from .models import Setting

logger = logging.getLogger("stland.runtime_config")


# --------------------------------------------------------------------------- #
# 域与子分组
# --------------------------------------------------------------------------- #
GROUP_SYSTEM = "system"
GROUP_SIM = "sim"

# 域的中文名。后台导航/页面标题用它，避免各处硬编码字符串。
GROUP_LABELS: dict[str, str] = {
    GROUP_SYSTEM: "系统配置",
    GROUP_SIM: "模拟器配置",
}

# 每个域下的子分组及展示顺序。后台按这个顺序堆卡片，
# 让「行情引擎 → 交易时段 → 结算 → 费用 → 账户」形成从市场到账户的阅读顺序。
GROUP_SECTIONS: dict[str, list[tuple[str, str]]] = {
    GROUP_SYSTEM: [
        ("security", "安全与访问控制"),
    ],
    GROUP_SIM: [
        ("engine", "行情引擎"),
        ("session", "交易时段"),
        ("settlement", "结算规则"),
        ("fees", "交易费用"),
        ("account", "账户默认值"),
    ],
}


def section_label(group: str, section: str) -> str:
    """子分组 key → 中文标题；未知分组退回 key 本身，避免渲染出空白标题。"""
    for key, label in GROUP_SECTIONS.get(group, []):
        if key == section:
            return label
    return section


@dataclass(frozen=True)
class Spec:
    """一个可调配置项的定义。"""

    key: str
    label: str
    kind: str            # float / int / bool / choice / text
    default: Any
    minimum: float = 0.0
    maximum: float = 0.0
    choices: tuple[str, ...] = ()
    unit: str = ""
    note: str = ""
    # 选项的展示名（value -> 中文标签）。为空时后台直接显示 value，
    # 交易模式这种「存 a_share、显示 A股」的项必须给标签，否则运营看不懂下拉框
    choice_labels: tuple[tuple[str, str], ...] = ()
    # 归属的域：system=系统配置、sim=模拟器配置。
    # 这是后台分页的唯一依据（原来的 page 字段已由它取代）。
    group: str = GROUP_SYSTEM
    # 域内的子分组 key，见 GROUP_SECTIONS。决定这一项落在哪个卡片里。
    section: str = "security"

    def coerce(self, raw: Any) -> Any:
        """把前端传来的字符串/数字规范成目标类型，越界抛 ValueError。"""
        if self.kind == "text":
            # 自由文本（如自定义时段表 JSON）。这里只做长度与基本格式校验，
            # 业务层的语义校验交给上层，因为运行期配置层不该知道
            # 「哪些 segment 合法」这种领域规则。
            text = str(raw if raw is not None else "").strip()
            if len(text) > self.maximum:
                raise ValueError(f"{self.label} 不能超过 {int(self.maximum)} 个字符")
            return text
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
        if self.kind == "text":
            return str(value or "")
        return float(value)


# 白名单：键名必须与 `settings` 上的属性同名，改完直接覆盖属性即生效
RUNTIME_SPECS: dict[str, Spec] = {
    # ======================= 模拟器配置 · 行情引擎 =======================
    # 这四项直接决定「这个模拟市场的价格怎么动」，与账户、费用完全无关。
    "engine_enabled": Spec(
        "engine_enabled", "行情引擎开关", "bool", True,
        note="关闭后行情冻结，便于排查问题",
        group=GROUP_SIM, section="engine",
    ),
    "tick_seconds": Spec(
        "tick_seconds", "行情推进间隔", "float", 1.0,
        minimum=0.2, maximum=60.0, unit="秒",
        note="越小行情跳动越快，CPU 占用也越高",
        group=GROUP_SIM, section="engine",
    ),
    "market_volatility": Spec(
        "market_volatility", "市场波动率系数", "float", 1.0,
        minimum=0.1, maximum=5.0,
        note="1.0 为基准；调大行情更活跃，调小更平稳（影响模拟引擎随机游走幅度）",
        group=GROUP_SIM, section="engine",
    ),
    "market_drift": Spec(
        "market_drift", "市场漂移系数", "float", 1.0,
        minimum=-3.0, maximum=3.0,
        note=">0 长期偏多，<0 长期偏空；0 表示无系统性方向",
        group=GROUP_SIM, section="engine",
    ),

    # ======================= 模拟器配置 · 交易时段 =======================
    "session_preset": Spec(
        "session_preset", "交易时段预设", "choice", "a_share",
        choices=("a_share", "us", "custom"),
        choice_labels=(
            ("a_share", "A股（含集合竞价与盘后固定价格）"),
            ("us", "美股（含盘前盘后）"),
            ("custom", "自定义时段表"),
        ),
        note="选 A股/美股 用内置时段表；选「自定义」后以下方时段表为准。调试模式下本项失效",
        group=GROUP_SIM, section="session",
    ),
    "session_slots": Spec(
        "session_slots", "自定义时段表", "text", "",
        maximum=4000,
        note='JSON 数组，按时间升序。每段：{"start":"09:30","end":"11:30","label":"上午连续竞价","segment":"continuous","match":true,"trade":true}。'
             'segment 取 pre/auction/continuous/lunch/post/fixed/closed；match=是否连续撮合，trade=是否受理申报',
        group=GROUP_SIM, section="session",
    ),

    # ======================= 模拟器配置 · 结算规则 =======================
    "trade_mode": Spec(
        "trade_mode", "结算模式", "choice", "t1",
        choices=("t1", "t0", "debug"),
        choice_labels=(
            ("t1", "T+1（当日买入次日可卖）"),
            ("t0", "T+0（当日买入当日可卖）"),
            ("debug", "调试模式（时段设置失效，全天可交易）"),
        ),
        note="T+1/T+0 只改结算规则；调试模式下忽略全部交易时段设置，全天可申报可撮合，并按 T+0 结算",
        group=GROUP_SIM, section="settlement",
    ),

    # ======================= 模拟器配置 · 交易费用 =======================
    "commission_rate": Spec(
        "commission_rate", "佣金费率", "float", 0.00025,
        minimum=0.0, maximum=0.003, note="万2.5 = 0.00025，双向收取",
        group=GROUP_SIM, section="fees",
    ),
    "commission_min": Spec(
        "commission_min", "最低佣金", "float", 5.0,
        minimum=0.0, maximum=100.0, unit="元", note="单笔佣金下限",
        group=GROUP_SIM, section="fees",
    ),
    "stamp_tax_rate": Spec(
        "stamp_tax_rate", "印花税率", "float", 0.0005,
        minimum=0.0, maximum=0.003, note="仅卖出收取，万5 = 0.0005",
        group=GROUP_SIM, section="fees",
    ),
    "transfer_fee_rate": Spec(
        "transfer_fee_rate", "过户费率", "float", 0.00001,
        minimum=0.0, maximum=0.001, note="双向收取，万0.1 = 0.00001",
        group=GROUP_SIM, section="fees",
    ),

    # ======================= 模拟器配置 · 账户默认值 =======================
    "seed_cash": Spec(
        "seed_cash", "新账户初始资金", "float", 1_000_000.0,
        minimum=0.0, maximum=1e9, unit="元", note="注册与批量建户默认值",
        group=GROUP_SIM, section="account",
    ),

    # ======================= 系统配置 · 安全与访问控制 =======================
    # 这两项与股市无关：换掉上面的模拟引擎它们照样成立，属于服务自身的安全边界。
    "signature_required": Spec(
        "signature_required", "请求签名校验", "bool", True,
        note="关闭后不再校验 HMAC 签名，仅建议联调期临时关闭",
        group=GROUP_SYSTEM, section="security",
    ),
    "login_fail_limit": Spec(
        "login_fail_limit", "登录失败锁定阈值", "int", 8,
        minimum=3, maximum=50, unit="次", note="连续失败达到该次数后锁定 5 分钟",
        group=GROUP_SYSTEM, section="security",
    ),
}


def keys_of(group: str | None = None) -> list[str]:
    """按域筛出配置键。`group=None` 返回全部，供不需要分页的调用方使用。"""
    if group is None:
        return list(RUNTIME_SPECS)
    return [k for k, s in RUNTIME_SPECS.items() if s.group == group]


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


def snapshot(group: str | None = None) -> list[dict]:
    """返回可调项的当前值 + 元信息，供后台渲染表单。

    `group` 为 None 时返回全部（保留给诊断/导出场景）；
    后台各页只取自己那个域，表单里就不会出现别的域的项目。
    每项带上 `group` / `section` / `section_label`，前端据此分卡片渲染，
    不必在前端再维护一份「哪个键属于哪一组」的映射。
    """
    items: list[dict] = []
    for key in keys_of(group):
        spec = RUNTIME_SPECS[key]
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
                "group": spec.group,
                "section": spec.section,
                "section_label": section_label(spec.group, spec.section),
            }
        )
    return items


def sections_of(group: str) -> list[dict]:
    """某个域的子分组清单（含标题与项数），供前端渲染分区卡片顺序。"""
    counts: dict[str, int] = {}
    for key in keys_of(group):
        section = RUNTIME_SPECS[key].section
        counts[section] = counts.get(section, 0) + 1
    return [
        {"key": key, "label": label, "count": counts.get(key, 0)}
        for key, label in GROUP_SECTIONS.get(group, [])
        if counts.get(key)
    ]


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


def update(
    session: Session,
    changes: dict[str, Any],
    operator: str = "admin",
    group: str | None = None,
) -> dict:
    """按键更新配置。返回 {updated: [...], rejected: [{key, reason}]}。

    `group` 非空时只接受该域内的键：后台两个页面各自提交自己的表单，
    如果前端因为缓存/标签页残留混进另一域的键，这里直接拒掉而不是静默写库 ——
    否则「在模拟器页保存」可能顺手改掉请求签名校验这种系统开关。
    """
    updated: list[str] = []
    rejected: list[dict] = []
    for key, raw in changes.items():
        spec = RUNTIME_SPECS.get(key)
        if spec is None:
            rejected.append({"key": key, "reason": "不支持修改该配置项"})
            continue
        if group is not None and spec.group != group:
            rejected.append(
                {"key": key, "reason": f"该配置项不属于「{GROUP_LABELS.get(group, group)}」"}
            )
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


def reset_defaults(session: Session, operator: str = "admin", group: str | None = None) -> dict:
    """把配置恢复为默认值，并清空库里的覆盖记录。

    `group` 非空时只重置该域：在「模拟器配置」页点恢复默认，
    不应该把「请求签名校验」也一并重置掉。
    注意用 `default_value` 而非 `spec.default`：默认值必须是**部署基线**
    （环境变量决定的取值），否则会把联调期关掉的开关重新打开。
    """
    keys = keys_of(group)
    for key in keys:
        apply_value(key, default_value(key))
        row = session.get(Setting, key)
        if row is not None:
            session.delete(row)
    session.commit()
    return {"ok": True, "count": len(keys), "group": group}
