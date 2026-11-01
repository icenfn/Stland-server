"""行情模拟引擎（自包含、无外部数据源）。

模型说明（详细推导与文献见 docs/research.md）：

1. **市场因子 + 个股贝塔**：收益分解为共同市场因子 ``m`` 与个股特质项 ``e``，
   ``r_i = beta_i * m + e_i``，对市场指数做均值回复（Ornstein–Uhlenbeck），
   个股叠加正负漂移。这与单因子/APT 资产定价框架一致，也解释了"同涨同跌"现象。
2. **波动率聚集**：特质项波动率随最近收益绝对值做 GARCH(1,1) 式更新，
   复现真实行情的波动率聚集（volatility clustering）与厚尾。
3. **跳变（jump）**：小概率事件触发跳跃，对应利空/利好突发，复现价格跳跃与涨跌停触及。
4. **价格离散化**：所有价格对齐 `tick_size` 并裁剪在涨跌停区间，复现 tick 网格导致的价格离散。
5. **成交量-收益联动**：|收益|越大、当日振幅越高，成交量放大（量价关系），
   同时叠加 U 型日内成交量曲线（开盘/收盘放量）。
6. **订单簿深度**：价差随瞬时波动放宽，深度随流动性参数变化，符合微观结构经验规律。

引擎不写入数据库热路径：tick 常驻内存（环形缓冲），分钟线由 tick 聚合，
日线在"日切"时落库，历史日线由确定性随机数回填（同一 seed 结果可复现）。
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from sqlmodel import select

from ..config import settings
from .book import Level, build_levels, clip, snap
from ..db import session_scope
from ..models import DailyBar, MinuteFrame, QuoteSnapshot, Stock, TradeSource
from .session import current_session, trading_days

# 一个「交易日」的模拟秒数：A 股 240 分钟。
# 所有波动率都换算成「每 tick 占一个交易日的比例」再做 sqrt 缩放，
# 这样 tick_seconds 从 1 秒调到 5 秒、日波动率仍然稳定在设定水平上。
DAY_SECONDS = 14400

logger = logging.getLogger("stland.market")


@dataclass
class MinuteBar:
    ts: int          # 分钟起点（epoch 秒）
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    # 该柱子所属时段：pre / auction / continuous / lunch / post / closed
    # 客户端据此把盘前、集合竞价、盘后单独着色，避免「一根连续的红绿柱里混了
    # 三段时间」这种对不上盘口的情况。
    segment: str = "continuous"


@dataclass
class SecurityState:
    code: str
    name: str
    prev_close: float
    price: float
    open: float
    high: float
    low: float
    volume: int
    amount: float
    tick_size: float
    limit_up: float
    limit_down: float
    beta: float
    drift: float
    vol: float
    liquidity: float
    float_shares: float
    # 当日「真实用户成交」的量与额，与 volume/amount 分开记：
    # volume 里可能混着模拟引擎 tick 的随机量，而 real_* 只由
    # apply_trade（用户撮合）累加。对外统计、日线落库、对账都用它，
    # 这样模拟量再大也不会污染真实成交口径。
    # 放在必填字段之后：dataclass 不允许默认值参数出现在必填参数之前。
    real_volume: int = 0
    real_amount: float = 0.0
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    last_close_date: str = ""
    # 当前状态属于哪个交易日；跨日后据此把前一日收成日线再归零。
    # 空串表示「还没走过日切」，首次 sync_day 只做归属、不算跨日。
    trade_date: str = ""
    # 涨跌幅上限（0.10 = ±10%）。日切换昨收后要靠它重算涨跌停价，
    # 否则次日的涨停价还停在昨天的价格上，价格一到就被错误裁住。
    limit_pct: float = 0.10
    minute_bars: deque[MinuteBar] = field(default_factory=lambda: deque(maxlen=1440))
    cur_minute: MinuteBar | None = None
    # 按自然日留存的分钟线，只服务「五日」视图。
    # minute_bars 的 maxlen 是 1440（正好一天），日切后昨天会被挤掉，
    # 单靠它拼不出跨日分时；这里按交易日单独归档，天然支持跨日连续轴。
    day_frames: dict[str, list] = field(default_factory=dict)

    def archive(self, bar, keep_days: int = 5) -> None:
        """把一根已收好的分钟线归档到所属自然日，只保留最近 keep_days 天。

        同时写一行 ``minute_frame``：内存字典在进程重启后不复存在，只有落库
        才能让「五日分时」在下次启动时仍有前四个交易日的数据。
        落库失败不影响行情主循环，因此单独吞掉异常（DB 短暂锁住是常见情况）。
        """
        key = time.strftime("%Y-%m-%d", time.localtime(bar.ts))
        self.day_frames.setdefault(key, []).append(bar)
        if len(self.day_frames) > keep_days:
            for stale in sorted(self.day_frames)[:-keep_days]:
                self.day_frames.pop(stale, None)
        try:
            with session_scope() as db:
                db.merge(
                    MinuteFrame(
                        key=f"{self.code}:{int(bar.ts)}",
                        code=self.code,
                        ts=int(bar.ts),
                        day=key,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=int(bar.volume),
                        amount=float(bar.amount),
                        segment=bar.segment,
                    )
                )
        except Exception:  # noqa: BLE001 - 归档失败不能打断撮合主循环
            pass
    cond_vol: float = 0.0           # GARCH 条件方差
    last_ret: float = 0.0           # 上一 tick 的对数收益（波动率聚集递推要用）
    day_volume_base: float = 0.0
    day_progress: float = 0.0       # 当日已走过的交易进度（0~1），量比要用

    # ---- 派生字段 ----
    @property
    def change(self) -> float:
        return round(self.price - self.prev_close, 4)

    @property
    def change_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return round((self.price / self.prev_close - 1) * 100, 4)

    @property
    def turnover_rate(self) -> float:
        if self.float_shares <= 0:
            return 0.0
        return round(self.volume / self.float_shares * 100, 4)

    @property
    def volume_ratio(self) -> float:
        """量比 = 当前每分钟均量 ÷ 过去 5 日每分钟均量。

        分子用「今日累计成交量 / 已交易分钟数」，分母用 `day_volume_base`
        （引擎里就是这只票的日均成交量基准）除以 240 分钟。
        开盘头几分钟分母极小，量比会异常放大，所以已交易分钟数至少按 1 分钟算，
        这也符合真实软件开盘量比普遍偏大的表现，但不会出现几十倍的假值。
        """
        if self.day_volume_base <= 0:
            return 0.0
        elapsed = max(self.day_progress * 240.0, 1.0)
        avg_per_min = self.volume / elapsed
        base_per_min = self.day_volume_base / 240.0
        if base_per_min <= 0:
            return 0.0
        return round(avg_per_min / base_per_min, 3)

    @property
    def amplitude(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return round((self.high - self.low) / self.prev_close * 100, 4)

    def quote(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "prev_close": self.prev_close,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "amount": round(self.amount, 2),
            "change": self.change,
            "change_pct": self.change_pct,
            "amplitude": self.amplitude,
            "turnover_rate": self.turnover_rate,
            "volume_ratio": self.volume_ratio,
            "limit_up": self.limit_up,
            "limit_down": self.limit_down,
            "bid": self.bids[0].price if self.bids else 0.0,
            "ask": self.asks[0].price if self.asks else 0.0,
            "bid_volume": self.bids[0].volume if self.bids else 0,
            "ask_volume": self.asks[0].volume if self.asks else 0,
            "is_limit_up": self.price >= self.limit_up - 1e-9,
            "is_limit_down": self.price <= self.limit_down + 1e-9,
            "ts": int(time.time() * 1000),
        }

    def depth(self) -> dict:
        """合成盘口：仅供「模拟引擎开着」时的兜底展示。

        真实模式下前端不再调它（见 routers/market.py 的 /depth），
        因为这里给出的是随机造出来的档位，不是任何人的真实挂单。
        """
        return {
            "code": self.code,
            "bids": [{"price": l.price, "volume": l.volume} for l in self.bids],
            "asks": [{"price": l.price, "volume": l.volume} for l in self.asks],
            "source": TradeSource.SIMULATED,
        }

    def apply_trade(self, price: float, quantity: int) -> None:
        """把一笔真实成交写回行情。

        原先 ``walk_book`` 只读盘口、不扣量，成交对行情没有任何影响 ——
        用户成交了，K 线却像什么都没发生。真实市场里成交本身就是价格来源，
        这里让成交推动 last / high / low / volume / amount，并据此重算涨跌停，
        使「用户之间的成交」能够自己走出趋势，而不必等引擎 tick。
        """
        qty = max(0, int(quantity))
        if qty <= 0 or price <= 0:
            return
        self.price = float(price)
        self.high = max(self.high, price)
        self.low = min(self.low, price) if self.low > 0 else price
        self.real_volume += int(quantity)
        self.real_amount += float(price) * int(quantity)
        self.volume += qty
        self.amount = round(self.amount + price * qty, 2)


class MarketEngine:
    """线程安全的模拟行情引擎。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, SecurityState] = {}
        self._index_minutes: deque[tuple[int, float]] = deque(maxlen=1440)
        # 3000 只是「成分等权比值 → 点位」的缩放基准，不是当前点位。
        # 真正生效的指数点位由引擎 index.py 按成分合成；这里的旧聚合值仅在
        # 有成分注册时才有意义，无成分时 index_quote() 会明确返回无数据。
        self._index_base: float = 3000.0
        self._index_prev_close: float = self._index_base
        self._index_value: float = self._index_base
        self._index_open: float = self._index_base
        self._index_high: float = self._index_base
        self._index_low: float = self._index_base
        self._market_ret: float = 0.0
        self._rng = random.Random(20260101)
        self._tick_count = 0
        self._day_key: str = ""
        self._last_tick_ts: float = 0.0
        # 当前 tick 所属时段（pre / auction / continuous / lunch / post / closed）
        # 与最近一次交易时钟快照，供 K 线与状态接口复用，避免重复计算
        self._segment: str = "continuous"
        self._session = None
        self.ready = False
        # 快照落库节流：行情循环每秒转一圈，每圈都写一次库在标的数量上来后
        # 会把 SQLite 写满。5 秒一次在「重启少丢几秒行情」和「写放大」之间够用了。
        self._last_persist_ts: float = 0.0

    # 快照落库的最小间隔（秒）
    PERSIST_INTERVAL = 5.0

    # ------------------------------------------------------------------ #
    # 注册标的
    # ------------------------------------------------------------------ #
    def register(
        self,
        *,
        code: str,
        name: str,
        init_price: float,
        tick_size: float,
        limit_up_pct: float,
        beta: float,
        drift: float,
        vol: float,
        liquidity: float,
        float_shares: float,
    ) -> None:
        with self._lock:
            prev_close = snap(init_price, tick_size)
            seed_vol = max(100.0, float_shares * 0.00002)
            bids, asks = build_levels(prev_close, tick_size, 5, 2, seed_vol, rng=self._rng)
            self._states[code] = SecurityState(
                code=code,
                name=name,
                prev_close=prev_close,
                price=prev_close,
                open=prev_close,
                high=prev_close,
                low=prev_close,
                volume=0,
                amount=0.0,
                tick_size=tick_size,
                limit_up=snap(prev_close * (1 + limit_up_pct), tick_size),
                limit_down=snap(prev_close * (1 - limit_up_pct), tick_size),
                beta=beta,
                drift=drift,
                vol=vol,
                liquidity=liquidity,
                float_shares=float_shares,
                limit_pct=limit_up_pct,
                bids=bids,
                asks=asks,
                # 条件方差初值 = 日波动率² ÷ 一天 tick 数（即单 tick 方差）
                cond_vol=(vol ** 2) / max(1.0, DAY_SECONDS / max(settings.tick_seconds, 0.2)),
                day_volume_base=max(1000.0, float_shares * 0.0006),
            )

    def unregister(self, code: str) -> bool:
        """下线一支标的：从内存引擎摘除，之后的行情/撮合都不再包含它。

        运营在后台删除标的时调用。不删库内记录的就绪状态，只摘内存状态，
        这样重启后若库里仍有该行，register_engine 会重新注册；
        若库里也已删除，则彻底不再出现。
        """
        with self._lock:
            return self._states.pop(code, None) is not None

    def registered(self) -> bool:
        return bool(self._states)

    # ------------------------------------------------------------------ #
    # 盘口
    # ------------------------------------------------------------------ #
    def _refresh_depth(
        self,
        st: SecurityState,
        rng: random.Random,
        *,
        intensity: float = 1.0,
        skew: float = 0.0,
    ) -> None:
        """重算某支标的的五档盘口。

        为什么单独抽出来：连续撮合、集合竞价、盘后固定价格交易三种情形都要
        刷新盘口（只是价格与成交量是否变化不同），下沉成一个方法后三条路径
        都走同一套档位生成逻辑，不会出现「某个时段档位量算错」。

        价差随瞬时波动放宽：波动越大，做市方越不敢把报价贴着中间价挂。
        涨停/跌停时再多加一档 —— 单边行情下对手盘更薄。
        """
        vol_ret = abs(st.last_ret) / (math.sqrt(max(st.cond_vol, 1e-14)) + 1e-12)
        spread_ticks = 1 + int(min(6, vol_ret * 2.0))
        if st.price >= st.limit_up - 1e-9 or st.price <= st.limit_down + 1e-9:
            spread_ticks += 1
        seed_vol = max(100.0, st.day_volume_base * 0.02 * st.liquidity)
        st.bids, st.asks = build_levels(
            st.price, st.tick_size, 5, spread_ticks, seed_vol,
            rng=rng, intensity=max(0.4, min(4.0, intensity)), skew=skew,
        )

    # ------------------------------------------------------------------ #
    # 历史回填
    # ------------------------------------------------------------------ #
    def backfill_daily(self, code: str, days: int = 240) -> list[dict]:
        """确定性随机游走生成历史日线（GBM + 波动率聚集），结果可复现。"""
        with self._lock:
            st = self._states.get(code)
            if st is None:
                return []
            rng = random.Random(f"stland-{code}")
            dates = trading_days(days)
            dt = 1 / 252
            # GARCH(1,1)：长期方差取「年化波动率² × dt」，权重 0.06/0.92/0.02 之和为 1，
            # 因此条件方差会围绕长期方差波动，不会漂移。
            #
            # 上一版这里写成 `0.02 * (path[-1] * 0.0 + 1)` —— 最后一项是**常数量 0.02**，
            # 相当于给方差加了一个与波动率无关的固定底盘，稳态方差被顶到 ≈0.25，
            # 折算年化波动率约 794%。反向回溯 240 天后 base 被放大到 1e15 量级，
            # 指数按「对首日收盘归一化」合成时比值趋近 0，整条日线归零。
            var_long = max((st.vol ** 2) * dt, 1e-8)
            cond = var_long
            prev_ret = 0.0
            price = st.prev_close
            # 反向生成：从当前价回溯过去，再翻转，保证最后一根接近今日价
            path: list[float] = [price]
            for _ in range(days):
                z = rng.gauss(0, 1)
                cond = 0.02 * var_long + 0.06 * (prev_ret ** 2) + 0.92 * cond
                sigma = max(0.002, math.sqrt(max(cond, 1e-12)))
                ret = st.drift * dt + sigma * z
                prev_ret = ret
                if rng.random() < 0.02:
                    ret += rng.choice([-1, 1]) * rng.uniform(0.02, 0.06)
                path.append(max(st.price * 0.15, path[-1] / math.exp(ret)))
            path.reverse()

            bars: list[dict] = []
            prev = path[0]
            for i, d in enumerate(dates):
                close = snap(path[i], st.tick_size)
                o = snap(prev * (1 + rng.gauss(0, 0.004)), st.tick_size)
                hi = snap(max(o, close) * (1 + abs(rng.gauss(0, 0.006))), st.tick_size)
                lo = snap(min(o, close) * (1 - abs(rng.gauss(0, 0.006))), st.tick_size)
                vol = int(st.day_volume_base * rng.uniform(0.6, 1.8))
                bars.append(
                    {
                        "code": code,
                        "trade_date": d,
                        "open": o,
                        "high": max(hi, o, close),
                        "low": min(lo, o, close),
                        "close": close,
                        "prev_close": snap(prev, st.tick_size),
                        "volume": vol,
                        "amount": round(vol * (o + close) / 2, 2),
                        # 这些数字是随机游走造出来的，只为让图表有形状。
                        # 必须标 backfill，否则会被当成用户真实成交历史。
                        "source": TradeSource.BACKFILL,
                    }
                )
                prev = close
            return bars

    # ------------------------------------------------------------------ #
    # 单个 tick
    # ------------------------------------------------------------------ #
    def tick(self) -> dict[str, dict]:
        """推进一个时间步，返回最新行情快照表。

        时钟给了两个正交能力位，这里分别使用：

        * ``is_open``（连续撮合）为真才**推进价格**并累计成交量 —— 集合竞价、
          午休、盘后固定价格交易都不该让 K 线继续长，否则收盘后图还在画；
        * ``can_trade``（可申报）为真就**刷新盘口** —— 集合竞价、盘后固定价格
          交易虽然不撮合，但委托在源源不断进来，五档挂单量必须跟着动，
          否则用户点开盘口看到的是几十分钟前的死数据。

        休市（两者皆假）时完全冻结，只回报快照。引擎关闭
        （``engine_enabled=False``）时同样冻结，便于运营排查问题。
        """
        with self._lock:
            now = time.time()
            if now - self._last_tick_ts < settings.tick_seconds * 0.5:
                return {c: s.quote() for c, s in self._states.items()}
            self._last_tick_ts = now

            session = current_session(now)
            self._session = session
            rng = self._rng

            # 休市：什么都不动，只回报快照
            if not session.is_open and not session.can_trade:
                self.ready = self.ready or bool(self._states)
                return {c: s.quote() for c, s in self._states.items()}

            # 只申报不撮合（集合竞价 / 盘后固定价格）：刷新盘口，价格与成交量不动
            if not session.is_open:
                for st in self._states.values():
                    self._refresh_depth(st, rng, intensity=1.0, skew=0.0)
                self.ready = self.ready or bool(self._states)
                return {c: s.quote() for c, s in self._states.items()}

            self._tick_count += 1
            self._segment = session.segment
            volatility = float(getattr(settings, "market_volatility", 1.0) or 1.0)
            drift_coef = float(getattr(settings, "market_drift", 1.0) or 0.0)

            # 时间步长 = 本 tick 占一个交易日的比例（不是占一年）。
            # 上一版误用年化口径（÷252 日）把方差压小了约 250 倍，
            # 每 tick 标准差只剩 1e-6 量级 —— 价格几乎从不越过一个最小报价
            # 单位，盘口与 K 线看起来就像「冻住了」，这是五档不更新的深层原因。
            dt = max(settings.tick_seconds, 0.2) / DAY_SECONDS

            # ---- 市场因子：OU 均值回复 ----
            # 日波动 1.1% 摊到一个交易日的 tick 数上：每 tick 标准差
            # = 0.011 × sqrt(tick_seconds / 14400)。用「交易日 14400 秒」而不是
            # 年化时间，纯为了口径直观 —— 一个交易日就是 240 分钟。
            tick_scale = math.sqrt(dt)
            m = -0.08 * self._market_ret + 0.011 * tick_scale * rng.gauss(0, 1) * volatility
            if rng.random() < 0.0006:
                m += rng.choice([-1, 1]) * rng.uniform(0.0004, 0.0012) * volatility
            self._market_ret = m

            for st in self._states.values():
                # ---- GARCH(1,1) 波动率聚集：条件方差按「上一期收益²」递推 ----
                var_long = max((st.vol ** 2) * dt, 1e-14)
                st.cond_vol = 0.02 * var_long + 0.06 * (st.last_ret ** 2) + 0.92 * st.cond_vol
                conn_vol = math.sqrt(max(st.cond_vol, 1e-14))
                idio = conn_vol * rng.gauss(0, 1) * volatility
                ret = st.beta * m + st.drift * drift_coef * dt + idio
                if rng.random() < 0.0004:
                    ret += rng.choice([-1, 1]) * rng.uniform(0.001, 0.0045) * volatility
                st.last_ret = ret

                new_price = snap(st.price * math.exp(ret), st.tick_size)

                # ---- 微观结构的 tick 噪声 ----
                # 真实盘口里即使没有净买卖压力，报价也会在相邻 tick 之间来回抖：
                # 这是做市商报价调整与零星成交的结果。没有这一项，低价股（tick 相对
                # 价格较粗）会连续几十个 tick 价格一模一样，图上是一条直线。
                tick_move_p = min(0.35, 0.08 * volatility)
                if new_price == st.price and rng.random() < tick_move_p:
                    step = st.tick_size
                    new_price = snap(st.price + (step if rng.random() < 0.5 else -step), st.tick_size)

                new_price = clip(new_price, st.limit_down, st.limit_up)
                st.price = new_price
                st.high = max(st.high, new_price)
                st.low = min(st.low, new_price)

                # ---- 量价关系 + U 型日内曲线 ----
                progress = (self._tick_count % 14400) / 14400
                st.day_progress = progress
                u_shape = 1.0 + 1.6 * (math.exp(-((progress - 0.02) ** 2) / 0.004) + math.exp(-((0.98 - progress) ** 2) / 0.004))
                intensity = (0.6 + 480 * abs(ret)) * st.liquidity * u_shape
                traded = int(max(100, st.day_volume_base * intensity / math.sqrt(14400 / max(settings.tick_seconds, 0.5))))
                traded = traded // 100 * 100
                st.volume += traded
                st.amount += traded * new_price

                # ---- 盘口：量随活跃度加深，方向随本 tick 收益倾斜 ----
                skew = max(-0.45, min(0.45, ret / (conn_vol * 2.0 + 1e-9) * 0.25))
                self._refresh_depth(st, rng, intensity=intensity, skew=skew)

                # ---- 分钟线聚合（带上时段标识，供客户端区分盘前/竞价/盘后） ----
                minute_ts = int(now // 60 * 60)
                if st.cur_minute is None or st.cur_minute.ts != minute_ts:
                    if st.cur_minute is not None:
                        st.minute_bars.append(st.cur_minute)
                        # 同时归档到当日帧，「五日」视图要靠它跨日拼接
                        st.archive(st.cur_minute)
                    st.cur_minute = MinuteBar(
                        minute_ts, st.price, st.price, st.price, st.price,
                        traded, traded * new_price, segment=self._segment,
                    )
                else:
                    cm = st.cur_minute
                    cm.high = max(cm.high, st.price)
                    cm.low = min(cm.low, st.price)
                    cm.close = st.price
                    cm.volume += traded
                    cm.amount += traded * new_price

            # ---- 指数 ----
            if self._states:
                self._index_value = round(
                    sum(s.price / s.prev_close for s in self._states.values()) / len(self._states) * self._index_prev_close, 2
                )
                self._index_high = max(self._index_high, self._index_value)
                self._index_low = min(self._index_low, self._index_value)
                mts = int(now // 60 * 60)
                if not self._index_minutes or self._index_minutes[-1][0] != mts:
                    self._index_minutes.append((mts, self._index_value))
                else:
                    self._index_minutes[-1] = (mts, self._index_value)

            self.ready = True
            return {c: s.quote() for c, s in self._states.items()}

    # ------------------------------------------------------------------ #
    # 持久化：启动水合 / 日切归档 / 快照落库
    # ------------------------------------------------------------------ #
    @staticmethod
    def _session_date(now: float | None = None) -> str:
        return current_session(time.time() if now is None else now).trade_date

    @staticmethod
    def _resnap_limits(st: "SecurityState") -> None:
        """按（新的）昨收与涨跌幅上限重算涨跌停价。

        日切时昨收变了，涨跌停必须跟着走。不重算的话，今天的涨停价还是
        昨天那个数 —— 次日高开后价格一路被「昨天的天花板」裁住，
        看起来像行情坏了，其实是限价没换。
        """
        pct = st.limit_pct if st.limit_pct > 0 else 0.10
        st.limit_up = snap(st.prev_close * (1 + pct), st.tick_size)
        st.limit_down = snap(st.prev_close * (1 - pct), st.tick_size)

    def hydrate(self) -> int:
        """从库内快照恢复行情状态，返回恢复的标的数。

        两种情形分开处理：
          · 快照就是今天的 → 原样恢复（重启不该丢掉当天已经走出来的行情）；
          · 快照是更早交易日的 → 只取它的收盘价当昨收，今日 OHLC 与成交归零
            （隔夜重启相当于新的一天，把昨天的成交量算进今天会让量比失真）。
        """
        today = self._session_date()
        try:
            with session_scope() as db:
                # 必须在会话内把行转成普通 dict：会话一关，ORM 对象的属性就
                # 过期了，出到外面再读会抛 DetachedInstanceError。
                rows = {
                    r.code: {
                        "trade_date": r.trade_date,
                        "prev_close": r.prev_close,
                        "price": r.price,
                        "open": r.open,
                        "high": r.high,
                        "low": r.low,
                        "volume": int(r.volume),
                        "amount": float(r.amount),
                    }
                    for r in db.exec(select(QuoteSnapshot)).all()
                }
        except Exception:  # noqa: BLE001 - 读不到快照就按初值跑，不能挡住启动
            logger.exception("读取行情快照失败，按初始值启动")
            return 0

        restored = 0
        with self._lock:
            for code, st in self._states.items():
                row = rows.get(code)
                if row is None:
                    st.trade_date = today
                    continue
                if row["trade_date"] == today and row["price"] > 0:
                    st.prev_close = row["prev_close"] or st.prev_close
                    st.price = row["price"]
                    st.open = row["open"] or row["price"]
                    st.high = row["high"] or row["price"]
                    st.low = row["low"] or row["price"]
                    st.volume = row["volume"]
                    st.amount = row["amount"]
                else:
                    # 更早的交易日：用收盘价做今天的昨收，其余归零
                    close = row["price"] or row["prev_close"]
                    if close <= 0:
                        st.trade_date = today
                        continue
                    st.prev_close = close
                    st.price = st.open = st.high = st.low = close
                    st.volume = 0
                    st.amount = 0.0
                st.trade_date = today
                st.last_ret = 0.0
                self._resnap_limits(st)
                self._refresh_depth(st, self._rng)
                restored += 1
        if restored:
            logger.info("已从快照恢复 %d 支标的的行情状态", restored)
        return restored

    def roll_day(self) -> list[dict]:
        """跨交易日时把上一日收成日线并归零，返回已归档的日线行。

        为什么要在引擎里做：昨收只能从「上一日收盘价」来。外部行情源接入后
        由 feeder 决定价格，但换日和归零仍得有人负责，放在引擎里与数据来源无关。
        """
        today = self._session_date()
        closed: list[dict] = []
        with self._lock:
            for st in self._states.values():
                if not st.trade_date:
                    st.trade_date = today
                    continue
                if st.trade_date == today:
                    continue
                # 判据用 real_volume 而不是 volume：模拟引擎 tick 也会累加
                # volume，若拿它判，开着引擎时每天都会为每支标的多写一根
                # 由随机量撑起来的假日线。real_volume 只由用户撮合累加。
                if st.real_volume > 0:
                    closed.append(
                        {
                            "code": st.code,
                            "trade_date": st.trade_date,
                            "open": st.open,
                            "high": st.high,
                            "low": st.low,
                            "close": st.price,
                            "prev_close": st.prev_close,
                            "volume": int(st.real_volume),
                            "amount": float(st.real_amount),
                            "source": TradeSource.REAL,
                        }
                    )
                st.prev_close = st.price
                st.trade_date = today
                st.price = st.open = st.high = st.low = st.prev_close
                st.volume = 0
                st.amount = 0.0
                st.real_volume = 0
                st.real_amount = 0.0
                st.last_ret = 0.0
                st.minute_bars.clear()
                st.cur_minute = None
                st.day_frames.clear()
                self._resnap_limits(st)
                self._refresh_depth(st, self._rng)

        if closed:
            self._write_daily(closed)
        return closed

    @staticmethod
    def _write_daily(rows: list[dict]) -> None:
        """把归档日线写进 ``daily_bar``（同 code+date 覆盖，不产生重复行）。"""
        try:
            with session_scope() as db:
                for row in rows:
                    hit = db.exec(
                        select(DailyBar)
                        .where(DailyBar.code == row["code"])
                        .where(DailyBar.trade_date == row["trade_date"])
                    ).first()
                    if hit is None:
                        db.add(DailyBar(**row))
                    else:
                        for key, val in row.items():
                            setattr(hit, key, val)
                        db.add(hit)
        except Exception:  # noqa: BLE001 - 日线归档失败不能打断行情循环
            logger.exception("归档日线失败")

    def today_bar(self, code: str) -> dict | None:
        """当前交易日的实时日线（尚未落库的那一根）。

        ``roll_day`` 只在跨日时写库，所以「今天」这根日线在收盘前一直不存在。
        日线/周线/月线图若不补它，用户当天成交了却看不到今天的 K 线 ——
        图停在昨天，看起来像行情没跟上交易。

        没有任何成交时返回 ``None``：开盘前不该先画一根 OHLC 全等于昨收的柱子。
        """
        with self._lock:
            st = self._states.get(code)
            # 用 real_volume 判断：开着模拟引擎时 tick 也会累加 volume，
            # 拿 volume 判断会让「只有引擎随机量、无真实成交」的日子也画出
            # 一根柱子，等于把模拟量当成今天的真实日线。
            if st is None or st.real_volume <= 0:
                return None
            return {
                "time": st.trade_date or self._session_date(),
                "open": st.open or st.price,
                "high": st.high or st.price,
                "low": st.low or st.price,
                "close": st.price,
                "volume": int(st.real_volume),
                "amount": round(float(st.real_amount), 2),
            }

    def persist_quotes(self, force: bool = False) -> int:
        """把当前行情状态落库（节流）。返回写入的标的数。

        先在锁内拍快照，出锁再写库：写库可能上百毫秒，占着引擎锁会让
        所有读行情的请求排队，盘口页面直接卡住。
        """
        now = time.time()
        if not force and now - self._last_persist_ts < self.PERSIST_INTERVAL:
            return 0
        self._last_persist_ts = now

        with self._lock:
            today = self._session_date(now)
            rows = [
                QuoteSnapshot(
                    code=st.code,
                    trade_date=st.trade_date or today,
                    prev_close=st.prev_close,
                    price=st.price,
                    open=st.open,
                    high=st.high,
                    low=st.low,
                    # 快照存「真实量优先」：有用户成交就记真实量并标 real，
                    # 否则退回引擎量并标 simulated。恢复时据此区分口径。
                    volume=int(st.real_volume or st.volume),
                    amount=float(st.real_amount or st.amount),
                    source=TradeSource.REAL if st.real_volume else TradeSource.SIMULATED,
                    updated_at=now,
                )
                for st in self._states.values()
            ]
        if not rows:
            return 0
        try:
            with session_scope() as db:
                for row in rows:
                    db.merge(row)
        except Exception:  # noqa: BLE001 - 快照落库失败只影响重启恢复，不能打断循环
            logger.exception("行情快照落库失败")
            return 0
        return len(rows)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def quotes(self) -> dict[str, dict]:
        with self._lock:
            return {c: s.quote() for c, s in self._states.items()}

    def quote(self, code: str) -> dict | None:
        with self._lock:
            st = self._states.get(code)
            return st.quote() if st else None

    def depth(self, code: str) -> dict | None:
        with self._lock:
            st = self._states.get(code)
            return st.depth() if st else None

    def price_of(self, code: str) -> float:
        with self._lock:
            st = self._states.get(code)
            return st.price if st else 0.0

    def apply_trade(self, code: str, price: float, quantity: int) -> None:
        """成交回写行情。无该标的报价时静默忽略（外部行情源下没有合成状态）。"""
        with self._lock:
            st = self._states.get(code)
            if st is not None:
                st.apply_trade(price, quantity)

    def minute_series(self, code: str, limit: int = 240, days: int = 1) -> list[dict]:
        """分时序列。days=1 取当日；days>1 取最近 N 个自然日首尾相接的连续轴。

        跨日时各日之间在时间上并不连续（隔夜、周末），所以除了时间戳，
        再补一个 `day` 字段：客户端靠它画日分隔线，并给每天单独画一条
        「当日开盘基准」虚线 —— 否则一条线跨了五天，看不出哪天是涨是跌。
        """
        with self._lock:
            st = self._states.get(code)
            if st is None:
                return []
            if days > 1:
                merged: list = []
                for key in sorted(st.day_frames)[-days:]:
                    merged.extend(st.day_frames[key])
                if st.cur_minute is not None:
                    merged.append(st.cur_minute)
                bars = merged
            else:
                bars = list(st.minute_bars)
                if st.cur_minute is not None:
                    bars.append(st.cur_minute)
            bars = bars[-limit:]
            out: list[dict] = []
            for b in bars:
                item = {
                    "t": b.ts,
                    "price": b.close,
                    "volume": b.volume,
                    "avg": round(b.amount / b.volume, 4) if b.volume else b.close,
                    "segment": b.segment,
                }
                # 单日视图不需要日期，多一个字段只会让客户端多一层判断
                if days > 1:
                    item["day"] = time.strftime("%Y-%m-%d", time.localtime(b.ts))
                out.append(item)
            return out

    def kline(self, code: str, period: str = "1min") -> list[dict]:
        """分钟级 K 线（1/5/15/30/60分钟），由分钟序列聚合。"""
        with self._lock:
            st = self._states.get(code)
            if st is None:
                return []
            bars = list(st.minute_bars)
            if st.cur_minute is not None:
                bars.append(st.cur_minute)
        # 120 分 = 半日线：A 股一天 240 分钟正好两根，用于「更多」里的 120 分
        step = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60, "120min": 120}.get(period, 1)
        if step == 1:
            grouped = bars
        else:
            grouped = []
            for i in range(0, len(bars), step):
                chunk = bars[i:i + step]
                if not chunk:
                    continue
                grouped.append(
                    MinuteBar(
                        ts=chunk[0].ts,
                        open=chunk[0].open,
                        high=max(b.high for b in chunk),
                        low=min(b.low for b in chunk),
                        close=chunk[-1].close,
                        volume=sum(b.volume for b in chunk),
                        amount=sum(b.amount for b in chunk),
                        # 聚合柱的时段按「首根」取：跨时段的分组本来就罕见，
                        # 取首根能保证开盘第一根集合竞价柱不被吞进连续竞价
                        segment=chunk[0].segment,
                    )
                )
        return [
            {
                "time": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": round(b.amount, 2),
                "segment": b.segment,
            }
            for b in grouped
        ]

    def index_quote(self) -> dict:
        """引擎内的旧综合指数快照（兜底用；前端主口径见 `engine/index.py`）。

        没有注册任何标的时**不存在点位**：返回 `has_data=False` 与 `None`，
        避免空库/未注入数据时首页显示一个凭空出现的点位。
        """
        with self._lock:
            if not self._states:
                return {
                    "code": "SL000001",
                    "name": "股岛综合指数",
                    "price": None,
                    "prev_close": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "change": None,
                    "change_pct": None,
                    "has_data": False,
                    "ts": int(time.time() * 1000),
                }
            change = round(self._index_value - self._index_prev_close, 2)
            return {
                "code": "SL000001",
                "name": "股岛综合指数",
                "price": self._index_value,
                "prev_close": self._index_prev_close,
                "open": self._index_open,
                "high": self._index_high,
                "low": self._index_low,
                "change": change,
                "change_pct": round(change / self._index_prev_close * 100, 2) if self._index_prev_close else 0.0,
                "has_data": True,
                "ts": int(time.time() * 1000),
            }

    def index_minutes(self) -> list[dict]:
        with self._lock:
            return [{"t": ts, "price": px} for ts, px in self._index_minutes]

    def stats(self) -> dict:
        with self._lock:
            up = sum(1 for s in self._states.values() if s.price > s.prev_close)
            down = sum(1 for s in self._states.values() if s.price < s.prev_close)
            total_amount = sum(s.amount for s in self._states.values())
            st = current_session()
            return {
                "tick": self._tick_count,
                "listed": len(self._states),
                "up": up,
                "down": down,
                "flat": len(self._states) - up - down,
                "total_amount": round(total_amount, 2),
                "index": self.index_quote(),
                "segment": st.segment,
            }

    def all_codes(self) -> list[str]:
        with self._lock:
            return list(self._states.keys())

    # ------------------------------------------------------------------ #
    # 引擎状态与运维控制（后台「模拟引擎」页签使用）
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """引擎运行态快照：开关、节奏、结算模式、交易时段与标的统计。"""
        from .session import normalize_mode

        with self._lock:
            st = current_session()
            listed = len(self._states)
            advancing = bool(settings.engine_enabled) and st.is_open
            return {
                "enabled": bool(settings.engine_enabled),
                "advancing": advancing,          # 此刻是否真的在推进价格
                "ready": self.ready,
                "tick_count": self._tick_count,
                "tick_seconds": settings.tick_seconds,
                "listed": listed,
                "segment": st.segment,
                "trade_mode": normalize_mode(getattr(settings, "trade_mode", None)),
                "session": st.to_dict(),
                "stats": {
                    "up": sum(1 for s in self._states.values() if s.price > s.prev_close),
                    "down": sum(1 for s in self._states.values() if s.price < s.prev_close),
                    "total_amount": round(sum(s.amount for s in self._states.values()), 2),
                },
            }

    def reset(self) -> dict:
        """重置内存中的行情状态，让所有标的回到昨收基准。

        后台排查「K 线明显跑飞」时用得上：清掉分钟线与累计成交，
        标的参数、持仓与账户资金都不动，所以不影响对账。
        """
        with self._lock:
            for s in self._states.values():
                s.price = s.prev_close
                s.open = s.prev_close
                s.high = s.prev_close
                s.low = s.prev_close
                s.volume = 0
                s.amount = 0.0
                s.minute_bars.clear()
                s.cur_minute = None
                s.cond_vol = 1e-4
            self._index_value = self._index_prev_close
            self._index_open = self._index_prev_close
            self._index_high = self._index_prev_close
            self._index_low = self._index_prev_close
            self._index_minutes.clear()
            self._market_ret = 0.0
            self._tick_count = 0
            return self.status()


market_engine = MarketEngine()
