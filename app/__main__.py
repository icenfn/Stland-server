"""最小可运行入口：python -m app（等价于 uvicorn app.main:app）。"""
from __future__ import annotations

import os

import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=int(os.getenv("PORT", settings.port)),
        reload=False,
        log_level="info",
    )
