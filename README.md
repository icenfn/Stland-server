# Stland 股岛 · 服务端

**自包含**股票交易系统服务端：不依赖任何外部 API，不接入任何外部行情数据源。行情、撮合、账务、风控、审计全部在本进程与 SQLite 内闭环完成。

配套客户端：[Stland](https://github.com/icenfn/Stland)（Tauri 2 + Vue 3）。

## 快速开始

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8999
```

首次启动自动完成：建表 → 写入 28 支内置标的 → 创建 3 类演示账户 → 生成 240 日历史日线 → 拉起行情引擎。

- API 文档：http://127.0.0.1:8999/docs
- 健康检查：http://127.0.0.1:8999/api/health

### 演示账户

| 用户名 | 口令 | 角色 | 初始资金 |
| --- | --- | --- | --- |
| `retail01` | `Stland@2026` | 零售 | 100 万 |
| `inst01` | `Stland@2026` | 机构 | 5000 万 + 2000 万融资额度 |
| `admin` | `Stland@Admin2026` | 管理 | — |

## 模块划分

```
app/
  config.py          环境变量配置（唯一配置来源，无外部服务依赖）
  security.py        JWT 签发校验 + HMAC-SHA256 请求签名 + nonce 防重放
  models.py          SQLModel 表：用户、账户、标的、持仓、委托、成交、审计
  db.py              SQLite 引擎与会话
  bootstrap.py       建表、种子数据、引擎启停
  engine/
    seed.py          28 支标的的静态定义（代码 / 名称 / 热词来源 / 行业 / 初始价）
    market.py        行情引擎：GBM + OU 市场因子 + GARCH 波动率聚集 + 跳跃
    book.py          订单簿：价格优先、同价时间优先、五档聚合
    session.py       A 股交易时段状态机（集合竞价 / 连续竞价 / 休市）
  services/
    trading.py       下单校验、撮合、成交、结算、撤单
    fees.py          佣金 / 印花税 / 过户费
    deps.py          认证依赖注入
  routers/
    auth.py          handshake / login / refresh / me
    market.py        overview / list / quote / kline / depth
    trade.py         order / cancel / orders / trades
    account.py       summary / positions / flows / risk / reset / risk-level
    admin.py         dashboard / users / audit / matching / settle / events
```

## 撮合与账务规则

**行情引擎**（`engine/market.py`）
价格演化 = 漂移 + 市场共因子 + 特质波动 + 跳跃项，波动率按 GARCH(1,1) 聚集，因此会出现「平静期后突然放量」的形态，而非纯随机游走。市场因子让 28 支标的呈现同涨同跌的相关性，符合真实盘面观感。所有随机源均可复现（固定种子）。

**订单簿**（`engine/book.py`）
价格优先、时间优先。市价单逐档吃单，以第五档为保护价，超出部分自动撤销，避免极端滑点。委托价格必须对齐 `tick_size`（0.01 元）且落在涨跌停区间内。

**资金冻结**
买入按「委托价 × 数量 + 预估费用」冻结；成交时已花费部分从冻结池核销，未花费部分退回可用资金。卖出冻结持仓，成交后扣减。

**T+1**
当日买入的持仓计入 `pending`，日终结转（`POST /api/admin/settle`）后解锁为可卖。

**费用**（`services/fees.py`）
佣金 0.025%（最低 5 元）+ 卖出印花税 0.05% + 过户费 0.001%。

## 安全

所有写接口校验 `X-Stland-Sign`：

```
HMAC-SHA256(sign_secret, METHOD \n PATH \n TS \n NONCE \n SHA256(BODY) \n USER_ID)
```

- 时间窗 60 秒（`STLAND_CLOCK_SKEW`）
- nonce 内存去重，防重放
- 登录失败 8 次锁定

> 生产部署务必替换 `STLAND_JWT_SECRET` 与 `STLAND_SIGN_SECRET`，并把 nonce 存储换成 Redis（当前为单实例内存态，不支持水平扩展）。

## 配置

全部通过环境变量，见 `.env.example`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `STLAND_PORT` | 8999 | 监听端口 |
| `STLAND_DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `STLAND_JWT_SECRET` | dev 值 | **生产必须替换** |
| `STLAND_SIGN_SECRET` | dev 值 | **生产必须替换** |
| `STLAND_SIGNATURE_REQUIRED` | true | 置 false 可关闭签名（仅联调用） |
| `STLAND_TICK_SECONDS` | 1 | 行情推进一步的时间间隔 |
| `STLAND_SESSION_MODE` | always | `always` 全天连续竞价；`real` 严格按 A 股时段 |
| `STLAND_HISTORY_DAYS` | 240 | 首次生成的历史日线天数 |
| `STLAND_SEED_CASH` | 1000000 | 演示账户初始资金 |

## 测试

```bash
pytest -q
```

覆盖：撮合优先级、市价单逐档吃单与保护价、tick 对齐校验、涨跌停拦截、费用计算、五档聚合、资金冻结释放。

CI（`.github/workflows/ci.yml`）额外执行启动冒烟：断言 28 支标的加载成功、健康检查通过、演示账户可登录。

## 关于 28 支内置标的

标的名取自 2025—2026 年中文互联网真实高热词（DeepSeek 深度求索、具身智能、情绪价值、萝卜纸巾猫等），映射到对应的产业叙事（国产大模型、人形机器人、情绪消费、宠物智能硬件……）。命名依据与检索来源见 [`docs/research.md`](docs/research.md)。

**价格全部由本地随机过程模拟生成，与任何真实上市公司无关，不构成投资建议。**
