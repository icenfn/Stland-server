"""Stland-server 服务入口。

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8999

自包含特性：
* 无外部行情数据源：全部行情由内置模拟引擎生成（GBM + OU 市场因子 + GARCH 波动率聚集 + 跳跃）；
* 无外部 API：账户、撮合、结算、风控全在 SQLite 内完成；
* 启动只建表并注册引擎，不写入任何业务数据；标的/账户/日线在后台面板显式初始化（/admin）。
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .bootstrap import bootstrap, start_engine, stop_engine
from .config import settings
from .engine.session import current_session
from .routers import account, admin, auth, market, stream, trade, watch
from .services import admin_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stland")


@asynccontextmanager
async def lifespan(app: FastAPI):
    info = bootstrap()
    logger.info("Stland 启动完成：%s", info)
    start_engine()
    logger.info("行情引擎已启动（tick=%ss, mode=%s）", settings.tick_seconds, settings.session_mode)
    try:
        yield
    finally:
        stop_engine()
        logger.info("行情引擎已停止")


app = FastAPI(
    title="Stland 股岛 API",
    description=(
        "自包含股票交易系统服务端：内置 28 支标的、模拟交易所撮合、账户与风控、"
        "管理端运营接口。所有行情与账务均在本地生成与结算，不依赖任何外部 API 或行情数据源。"
    ),
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# 移动端为 Tauri WebView 容器（origin 为 tauri://localhost 或 https://tauri.localhost）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_meta(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    response.headers["X-Stland-Version"] = settings.app_version
    response.headers["X-Stland-Server-Time"] = str(int(time.time()))
    response.headers["X-Process-Time"] = f"{(time.time() - start) * 1000:.1f}ms"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "INTERNAL_ERROR", "message": "服务端内部错误，请稍后重试"}},
    )


# 运营后台：Jinja2 服务端渲染
#
# 为什么从「一个 1196 行的静态 HTML」改成模板：
#   十个分区挤在同一个文件里，改一页要在上千行里找锚点，还容易误伤别的分区；
#   拆成 base + 每页一个模板之后，分区边界由文件系统体现，
#   导航清单只写一处，增删分区不必再同步 HTML 与 JS 两个地方。
_TEMPLATES = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))

# 侧边导航清单：顺序即菜单顺序，key 同时是模板文件名与 URL hash
# icon 取 Material Icons 的 ligature 名，由 base.html 引入的字体渲染
ADMIN_NAV = [
    # 两级导航：一级为分组（有 children 时渲染成可折叠分组），二级为真实页面。
    # 没有 children 的条目本身就是页面，直接跳转。
    # icon 只能从 mdui 1.x 自带 Material Icons 字体里确实带连字的名字中选：
    # admin_panel_settings / candlestick_chart / settings_suggest / shield / database
    # 这几个较新的图标名在 1.x 字体里没有字形，会被原样显示成文字
    {"key": "overview", "label": "总览", "icon": "dashboard"},
    {
        "key": "accounts",
        "label": "账户管理",
        "icon": "people",
        "children": [
            {"key": "retail", "label": "零售账户", "icon": "person"},
            {"key": "institution", "label": "机构账户", "icon": "business"},
            {"key": "admins", "label": "管理员", "icon": "supervisor_account"},
        ],
    },
    {"key": "stocks", "label": "标的与指数", "icon": "show_chart"},
    {"key": "boards", "label": "板块管理", "icon": "view_column"},
    {"key": "industries", "label": "行业管理", "icon": "category"},
    {
        "key": "simulator",
        "label": "模拟器",
        "icon": "settings_backup_restore",
        "children": [
            {"key": "engine", "label": "模拟引擎", "icon": "settings_backup_restore"},
            {"key": "simcfg", "label": "模拟器配置", "icon": "tune"},
            {"key": "data", "label": "数据初始化", "icon": "storage"},
        ],
    },
    {"key": "ops", "label": "撮合质量", "icon": "swap_horiz"},
    {"key": "config", "label": "系统配置", "icon": "tune"},
]

# 后台自己的 css/js 与 mdui、Material Icons 字体都从 /static 出
_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ---------------------------------------------------------------------------
# 管理后台登录态
# ---------------------------------------------------------------------------
# 会话表与 Cookie 读写都收敛在 services/admin_session.py：
# 那里是「登录写会话」与「鉴权读会话」唯一的共同状态源。
# 之前这部分逻辑只写在 main.py，而 admin_user 依赖仍只认 Bearer 令牌，
# 于是登录成功也进不去面板（每个接口都回「缺少访问令牌」）。


def _admin_logged_in(request: Request) -> bool:
    """该请求是否带着有效的后台登录态。"""
    return admin_session.logged_in(request)


@app.get("/admin/logout", include_in_schema=False)
def admin_logout(request: Request):
    """退出登录：撤销内存会话并清除 Cookie。"""
    admin_session.drop_session(admin_session.request_sid(request))
    resp = RedirectResponse("/admin/login", status_code=302)
    admin_session.clear_cookie(resp)
    return resp


@app.get("/admin/login", include_in_schema=False)
def admin_login_page(request: Request):
    """独立登录页：不套后台面板的壳，避免登录框被应用栏与侧边栏挤在里面。"""
    return templates.TemplateResponse(request, "admin/login_page.html", {})


@app.get("/admin", include_in_schema=False)
def admin_panel(request: Request):
    # 面板本身只是一张静态壳，但它会拉取 /api/admin/* 数据；
    # 不做这层跳转的话，未登录用户能直接看到空白面板并触发一串 401。
    if not _admin_logged_in(request):
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {"nav": ADMIN_NAV, "version": settings.app_version},
    )

app.include_router(auth.router)
app.include_router(market.router)
app.include_router(trade.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(watch.router)
app.include_router(stream.router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "app": "Stland 股岛",
        "version": settings.app_version,
        "docs": "/docs",
        "port": settings.port,
        "endpoints": [
            "/api/auth/handshake",
            "/api/auth/login",
            "/api/market/overview",
            "/api/market/list",
            "/api/trade/order",
            "/api/account/summary",
            "/api/watch/items",
            "/api/market/index",
            "/api/admin/dashboard",
        ],
        "admin_panel": "/admin/",
    }


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    from .engine.market import market_engine

    st = current_session()
    return {
        "status": "ok",
        "version": settings.app_version,
        "server_time": int(time.time()),
        "engine": {
            "ready": market_engine.ready,
            "listed": len(market_engine.all_codes()),
            "tick_seconds": settings.tick_seconds,
        },
        "session": st.to_dict(),
    }
