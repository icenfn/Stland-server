"""管理端路由包：按域拆分的子模块共同注册到 `/api/v1/admin`。

拆分原则是「一个子模块 = 一个运营页签背后的接口」：

| 子模块 | 页签 / 职责 |
| --- | --- |
| `auth` | 管理员登录、当前身份、改密 |
| `dashboard` | 运营总览（资产、成交、热门标的） |
| `settings` | 运行期可调配置（费率、交易时段、风控阈值…） |
| `accounts` | 交易账户：列表 / 开户 / 改户 / 冻结 / 调账 |
| `admins` | 管理员账户：增删与启停 |
| `market_data` | 标的、板块、行业的维护 |
| `seed` | 手动注入标准数据集与进度查询 |
| `engine` | 模拟引擎（撮合引擎）的开关、重置、状态 |
| `ops` | 撮合质量统计与对账 |

导入顺序即时 OpenAPI 里的端点顺序，与拆分前的文件顺序保持一致。
"""
from __future__ import annotations

from ._common import router

# 导入即注册：顺序不要随意调整，否则 /docs 的端点次序会变。
from . import auth  # noqa: F401,E402
from . import dashboard  # noqa: F401,E402
from . import settings  # noqa: F401,E402
from . import accounts  # noqa: F401,E402
from . import admins  # noqa: F401,E402
from . import market_data  # noqa: F401,E402
from . import seed  # noqa: F401,E402
from . import engine  # noqa: F401,E402
from . import ops  # noqa: F401,E402

__all__ = ["router"]
