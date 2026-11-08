"""API 版本前缀与旧路径兼容。

服务端对外只暴露一个版本化前缀 `/api/v1/*`。但客户端是装在用户手机里的，
升级节奏不由服务端控制，因此保留一条**单向别名**：把 `/api/xxx` 内部重写成
`/api/v1/xxx`，老安装包不改造也能继续用。

这样做而不是「每个路由注册两次」，是因为重写发生在 ASGI 层、路由表只有一份：
OpenAPI 里不会出现重复端点，将来要下线兼容只要删掉这个中间件。
"""
from __future__ import annotations


class ApiVersionAlias:
    """纯 ASGI 中间件：在路由匹配前改写 `scope["path"]`。"""

    def __init__(self, app, prefix: str = "/api", version: str = "v1") -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.version = version
        self.versioned = f"{self.prefix}/{version}"

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                if not (path == self.versioned or path.startswith(self.versioned + "/")):
                    scope = dict(scope)
                    scope["path"] = self.versioned + path[len(self.prefix):]
                    scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)
