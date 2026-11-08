"""统一错误契约。

对外所有错误响应都收敛成同一形状：

```json
{"detail": {"code": "SOME_CODE", "message": "人话说明", "trace_id": "...", "fields": {...}}}
```

为什么还要 `detail` 这一层：FastAPI 的 `HTTPException` 天然把它作为载荷字段，
保留它可以让既有客户端（以及 OpenAPI 文档）继续工作，同时把内部结构从
「有时是字符串、有时是对象」统一为稳定的对象。`fields` 只在参数校验失败时出现。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("stland.errors")

# HTTP 状态码 → 缺省错误码（仅在抛错方没有显式给 code 时使用）
_STATUS_CODE = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    423: "LOCKED",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(Exception):
    """业务异常：抛出后由全局处理器渲染成统一错误体。

    比 `HTTPException` 好在两点：语义明确（这不是 HTTP 层错误），
    并且可以附带 `fields` 表达「哪些入参不合法」。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        fields: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = dict(fields) if fields else None
        self.headers = dict(headers) if headers else None


def error_body(
    code: str,
    message: str,
    *,
    trace_id: str = "",
    fields: Mapping[str, Any] | None = None,
) -> dict:
    detail: dict[str, Any] = {"code": code, "message": message}
    if trace_id:
        detail["trace_id"] = trace_id
    if fields:
        detail["fields"] = dict(fields)
    return {"detail": detail}


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "") or ""


def _normalize(detail: Any, status_code: int) -> tuple[str, str, Mapping[str, Any] | None]:
    """把历史上三种 `detail` 写法收敛为 (code, message, fields)。"""
    if isinstance(detail, dict):
        code = str(detail.get("code") or _STATUS_CODE.get(status_code, "ERROR"))
        message = str(detail.get("message") or detail.get("detail") or code)
        fields = detail.get("fields")
        return code, message, fields if isinstance(fields, Mapping) else None
    if isinstance(detail, str) and detail:
        return _STATUS_CODE.get(status_code, "ERROR"), detail, None
    return _STATUS_CODE.get(status_code, "ERROR"), "请求失败", None


def install_error_handlers(app: FastAPI) -> None:
    """把三类异常统一挂到同一个渲染函数上。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, trace_id=_trace_id(request), fields=exc.fields),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message, fields = _normalize(exc.detail, exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, trace_id=_trace_id(request), fields=fields),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {
            ".".join(str(p) for p in err.get("loc", ()) if p != "body") or "body": err.get("msg", "")
            for err in exc.errors()
        }
        return JSONResponse(
            status_code=422,
            content=error_body(
                "VALIDATION_ERROR",
                "请求参数校验失败",
                trace_id=_trace_id(request),
                fields=fields,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body("INTERNAL_ERROR", "服务端内部错误，请稍后重试", trace_id=_trace_id(request)),
        )
