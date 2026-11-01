# Stland 股岛 · 服务端

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white">
</p>

自包含的股票交易系统服务端。行情、撮合、账务、风控、审计全部在本进程与 SQLite 内闭环完成，**不依赖任何外部 API，也不接入任何外部行情数据源** —— 克隆下来就能跑出一个有分时、有五档、能下单、能结算的完整盘面。

配套客户端：[Stland](https://github.com/icenfn/Stland)（Tauri 2 + Vue 3）。

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [演示账户](#演示账户)
- [架构](#架构)
- [核心设计](#核心设计)
- [运营后台](#运营后台)
- [配置](#配置)
- [测试与 CI](#测试与-ci)
- [免责声明](#免责声明)

## 特性

| | |
| --- | --- |
| **模拟行情引擎** | 漂移 + 市场共因子 + 特质波动 + 跳跃项，GARCH(1,1) 波动率聚集；随机源固定种子，可逐 tick 复现 |
| **真实撮合** | 价格优先、同价时间优先的价格-时间优先队列，市价单逐档吃单并以第五档为保护价 |
| **完整账务** | 买入冻结、成交核销、卖出冻结持仓、T+1 结转、佣金/印花税/过户费三项费用 |
| **指数合成** | 成分股等权归一化合成指数与分时，无成分报价时不虚构点位 |
| **A 股时段机** | 集合竞价 / 连续竞价 / 午休 / 休市状态机，支持 `a_share` / `us` / `custom` 预设 |
| **在线配置** | 费率、结算模式、行情强度等运营参数存库热更新，改完下一次调用即生效 |
| **运营后台** | Jinja2 服务端渲染的单页后台，无需前端构建，开箱可用 |
| **安全** | PBKDF2 口令散列、HS256 JWT + 即时吊销、HMAC 请求签名与 nonce 防重放 |

## 快速开始

需要 Python 3.11 及以上。

```bash
git clone https://github.com/icenfn/Stland-server.git
cd Stland-server

pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8999
```

首次启动会自动完成：建表 → 写入内置标的 → 创建演示账户 → 补齐历史日线 → 拉起行情引擎。

| 入口 | 地址 |
| --- | --- |
| 运营后台 | http://127.0.0.1:8999/admin |
| API 文档 | http://127.0.0.1:8999/docs |
| 健康检查 | http://127.0.0.1:8999/api/health |

> 只想看一眼盘面的话，`/admin` 登录后即可查看行情、标的与账户，不需要构建任何前端。

## 演示账户

| 用户名 | 口令 | 角色 | 初始资金 |
| --- | --- | --- | --- |
| `retail01` | `Stland@2026` | 零售 | 100 万 |
| `inst01` | `Stland@2026` | 机构 | 5000 万 + 2000 万授信 |
| `admin` | `Stland@Admin2026` | 管理员 | — |

> 以上仅为本地演示凭据。**部署到任何可访问的环境前，请先修改管理员口令并替换密钥。**

## 架构

```
app/
├── main.py            应用装配、中间件、后台路由注册
├── config.py          部署期配置：环境变量 → Settings（默认值唯一来源）
├── runtime_config.py  运行期配置：白名单键、类型/范围校验、写库即热生效
├── security.py        口令散列、JWT 签发校验、HMAC 请求签名、nonce 防重放
├── models.py          SQLModel 表定义
├── db.py              引擎与会话
├── bootstrap.py       建表、种子注入、历史回填、引擎启停
│
├── engine/            纯计算层，不碰 HTTP
│   ├── seed.py        内置标的静态定义
│   ├── market.py      行情引擎（随机过程 + 聚合快照）
│   ├── book.py        订单簿与五档聚合
│   ├── index.py       指数与成分股合成
│   ├── periods.py     日线 / 周线 / 月线聚合
│   └── session.py     交易时段状态机
│
├── services/          业务编排层
│   ├── trading.py     下单校验、撮合、成交、结算、撤单
│   ├── fees.py        佣金 / 印花税 / 过户费
│   ├── deps.py        认证依赖注入
│   └── admin_session.py  后台会话读写
│
├── routers/           HTTP 层
│   ├── auth.py        handshake / login / refresh / me
│   ├── market.py      overview / list / quote / kline / depth / index
│   ├── trade.py       order / cancel / orders / trades
│   ├── account.py     summary / positions / flows / risk
│   ├── watch.py       自选
│   ├── stream.py      SSE 推送
│   └── admin.py       后台接口 + 页面路由
│
└── templates/admin/   运营后台（base + 每页一个子模板）
```

分层约定：`engine` 只做计算、不碰数据库与 HTTP；`services` 编排事务；`routers` 只负责校验入参与序列化。这样引擎可以脱离 Web 单独跑测试。

## 核心设计

**行情演化** `engine/market.py`
价格 = 漂移项 + 市场共因子 + 特质波动 + 跳跃项，波动率按 GARCH(1,1) 聚集，因此会出现「平静期后突然放量」的形态，而不是纯随机游走。共因子让各标的呈现同涨同跌的相关性。

**订单簿** `engine/book.py`
价格优先、同价时间优先。市价单逐档吃单，以第五档为保护价，超出部分自动撤销，避免极端滑点。委托价必须对齐 `tick_size` 且落在涨跌停区间内。

**指数** `engine/index.py`
由成分股按等权归一化价格合成：`Σ(price_i / prev_close_i) / n × 基准点位`。等权而非市值加权 —— 模拟盘标的股本参数差异极大，市值加权会让单只标的过度主导。**基准点位只是缩放系数**：没有可用成分报价时返回 `has_data=false` 且点位为 `None`，绝不拿常数兜底冒充行情。

**资金与结算**
买入按「委托价 × 数量 + 预估费用」冻结；成交时已花费部分从冻结池核销，余额退回可用。卖出冻结持仓，成交后扣减。当日买入计入 `pending`，日终结转后解锁为可卖（T+1）。

**费用** `services/fees.py`
默认佣金 0.025%（最低 5 元）+ 卖出印花税 0.05% + 过户费 0.001%，均可在线调整。

**安全** `security.py`
所有写接口校验 `X-Stland-Sign`：

```
HMAC-SHA256(sign_secret, METHOD \n PATH \n TS \n NONCE \n SHA256(BODY) \n USER_ID)
```

`PATH` 不含查询串（与 FastAPI `request.url.path` 对齐）；时间窗默认 60 秒；nonce 去重防重放；登录失败 8 次锁定。

> nonce 与锁定计数当前为单实例内存态，**不支持水平扩展**。多副本部署前请换成 Redis。

## 运营后台

服务端渲染，无需前端构建。左侧按域分组，覆盖日常运营的全部操作：

| 分组 | 页面 |
| --- | --- |
| 概览 | 平台 KPI、撮合质量、异常事件 |
| 行情数据 | **标的管理**（增删改、上下架）、**指数管理**（成分与点位，只读） |
| 市场规则 | 板块、行业 |
| 账户 | 零售账户、机构账户 —— 支持编辑名称、状态、风险偏好、授信额度、佣金、重置口令，并可顺带调整资金 |
| 模拟器 | 引擎参数、运行期配置、数据初始化 |
| 系统 | 管理员、审计日志、运维操作 |

账户编辑采用「显式传入才修改」的语义：表单没提交的字段保持原值，留空明确表示「跟随角色默认」，因此不会因为界面只展示了部分字段而把其余字段清空。资金变动仍写一条审计流水，与旧的「调资金」口径一致。

## 配置

配置分两层，避免本应长期稳定的部署参数被日常运营误改：

- `config.py` —— **部署期**配置，来自环境变量，是**默认值的唯一来源**；
- `runtime_config.py` —— **运行期**配置，后台可改、写库即生效，只开放白名单键并逐项校验类型与取值范围。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `STLAND_HOST` | `0.0.0.0` | 监听地址 |
| `STLAND_PORT` | `8999` | 监听端口 |
| `STLAND_DATA_DIR` | `./data` | 数据目录（SQLite 与运行数据） |
| `STLAND_DB_URL` | `sqlite:///<data_dir>/stland.db` | 数据库连接串 |
| `STLAND_JWT_SECRET` | dev 值 | **生产必须替换** |
| `STLAND_SIGN_SECRET` | dev 值 | **生产必须替换** |
| `STLAND_ACCESS_TOKEN_TTL` | `7200` | 访问令牌有效期（秒） |
| `STLAND_REFRESH_TOKEN_TTL` | 14 天 | 刷新令牌有效期（秒） |
| `STLAND_CLOCK_SKEW` | `60` | 签名时间窗（秒） |
| `STLAND_SIGNATURE_REQUIRED` | `true` | 置 `false` 关闭签名校验（仅联调） |
| `STLAND_LOGIN_FAIL_LIMIT` | `8` | 登录失败锁定阈值 |
| `STLAND_SEED_CASH` | `1000000` | 演示账户初始资金 |
| `STLAND_COMMISSION_RATE` | `0.00025` | 佣金费率 |
| `STLAND_COMMISSION_MIN` | `5.0` | 最低佣金 |
| `STLAND_STAMP_TAX_RATE` | `0.0005` | 印花税（卖出） |
| `STLAND_TRANSFER_FEE_RATE` | `0.00001` | 过户费 |
| `STLAND_TICK_SECONDS` | `1` | 行情推进一步间隔（秒） |
| `STLAND_TRADE_MODE` | `t1` | 结算模式：`t1` / `t0` / `debug` |
| `STLAND_SESSION_SLOTS` | A股内置时段 | 交易时段 JSON 时段表（唯一来源，后台可一键填入 A股/美股 模板） |
| `STLAND_MARKET_VOLATILITY` | `1.0` | 波动强度系数 |
| `STLAND_MARKET_DRIFT` | `1.0` | 漂移强度系数 |
| `STLAND_ENGINE_ENABLED` | `false` | 启动时是否推进行情（默认关闭，需在后台手动开启） |
| `STLAND_FORCE_OPEN` | `false` | 忽略时段强制可交易（调试用） |

完整示例见 [`.env.example`](.env.example)。

### 运行期配置

`runtime_config.py` 只声明「哪些键可改、类型、范围、文案、归属分组」，默认值一律从 `config.Settings` 继承，不在两处各写一份。

进程启动时会把 `settings` 的当时取值记为**部署基线**，「恢复默认」回到的是基线而不是代码常量 —— 否则联调期用 `STLAND_SIGNATURE_REQUIRED=false` 关掉的签名校验会被一键打开，客户端随即整片 400。

## 测试与 CI

```bash
pytest -q
```

覆盖撮合优先级、市价单逐档吃单与保护价、tick 对齐、涨跌停拦截、费用计算、五档聚合、资金冻结与释放。

`.github/workflows/ci.yml` 额外执行启动冒烟：断言内置标的加载成功、健康检查通过、演示账户可登录。

## 关于内置标的

标的名取自 2025—2026 年中文互联网真实高热词（DeepSeek、具身智能、情绪价值、萝卜纸巾猫等），映射到对应产业叙事（国产大模型、人形机器人、情绪消费、宠物智能硬件……）。命名依据与检索来源见 [`docs/research.md`](docs/research.md)。

**价格全部由本地随机过程模拟生成，与任何真实上市公司无关。**

## 免责声明

本项目用于教学与工程演示，不构成任何投资建议。内置行情、账户与成交数据均为模拟产物，不可用于真实交易决策。
