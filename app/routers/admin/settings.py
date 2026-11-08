"""管理端 · 运行期配置。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 运行期配置
# --------------------------------------------------------------------------- #
def _check_group(group: str) -> str:
    """校验域参数，把非法值挡在业务之前。"""
    if group not in runtime_config.GROUP_LABELS:
        raise HTTPException(
            400,
            detail={
                "code": "BAD_GROUP",
                "message": f"group 只接受 {'/'.join(runtime_config.GROUP_LABELS)}",
            },
        )
    return group


@router.get("/settings")
def get_settings(
    group: str = Query(default="", max_length=16),
    admin: Admin = Depends(admin_user),
) -> dict:
    """可调配置项及其当前值（含类型、范围、说明，前端据此渲染表单）。

    传 `group` 只返回该域：后台「系统配置」拿 system、
    「模拟配置」拿 sim，各自页面不会出现对方域的项目。
    不传时返回全部，保留给诊断与导出场景。
    """
    scope = _check_group(group) if group else None
    return {
        "items": runtime_config.snapshot(scope),
        "group": scope,
        "sections": runtime_config.sections_of(scope) if scope else [],
        "groups": [
            {"key": key, "label": label}
            for key, label in runtime_config.GROUP_LABELS.items()
        ],
        # 「交易时段」上方的填充按钮：后端出模板，前端不另抄一份时间表，
        # 以后调整内置时段只改 session.py 的 PRESETS。
        "slot_presets": [
            {"key": key, "label": PRESETS[key]["label"], "json": preset_slots_json(key)}
            for key in FILLABLE_PRESETS
        ],
        # 「休市日期」上方的填充按钮，同样由后端出模板（session.py 的
        # HOLIDAY_PRESETS），前端不另抄一份节假日表。
        "holiday_presets": [
            {"key": key, "label": conf["label"], "json": holiday_preset_json(key)}
            for key, conf in HOLIDAY_PRESETS.items()
        ],
    }


class SettingsIn(BaseModel):
    """批量更新：只传要改的键，未传的保持原值。"""

    values: dict[str, object] = Field(default_factory=dict)


@router.put("/settings")
def put_settings(
    payload: SettingsIn,
    group: str = Query(default="", max_length=16),
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """修改运行期配置：校验通过后立即写到内存 settings，下一次请求即生效。

    带 `group` 时只接受该域内的键。两个页面各自提交自己的表单，
    这样即使前端状态串了（比如同时开着两个标签页），
    在「模拟配置」页保存也不可能顺手改掉「请求签名校验」这种系统开关。
    """
    if not payload.values:
        raise HTTPException(400, detail={"code": "EMPTY", "message": "没有需要修改的配置"})
    scope = _check_group(group) if group else None
    return runtime_config.update(
        session, payload.values, operator=admin.username, group=scope
    )


@router.post("/settings/reset")
def reset_settings(
    group: str = Query(default="", max_length=16),
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """恢复默认值。带 `group` 时只重置该域。

    在「模拟配置」页点恢复默认，不应把「请求签名校验」这类系统开关
    一起重置 —— 那会让整个客户端因为签名被重新要求而整片报错。
    """
    scope = _check_group(group) if group else None
    return runtime_config.reset_defaults(session, operator=admin.username, group=scope)
