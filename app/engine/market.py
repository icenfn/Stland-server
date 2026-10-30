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

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..config import settings
from .book import Level, build_levels, clip, snap
from .session import current_session, trading_days

YEAR_SECONDS = 252 * 4 * 3600  # 一个"交易日"按 4 小时计


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
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    last_close_date: str = ""
    minute_bars: deque[MinuteBar] = field(default_factory=lambda: deque(maxlen=1440))
    cur_minute: MinuteBar | None = None
    cond_vol: float = 0.0           # GARCH 条件方差
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
        return {
            "code": self.code,
            "bids": [{"price": l.price, "volume": l.volume} for l in self.bids],
            "asks": [{"price": l.price, "volume": l.volume} for l in self.asks],
        }


class MarketEngine:
    """线程安全的模拟行情引擎。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, SecurityState] = {}
        self._index_minutes: deque[tuple[int, float]] = deque(maxlen=1440)
        self._index_prev_close: float = 3000.0
        self._index_value: float = 3000.0
        self._index_open: float = 3000.0
        self._index_high: float = 3000.0
        self._index_low: float = 3000.0
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
            seed_vol = max(1.0, float_shares * 0.00002)
            bids, asks = build_levels(prev_close, tick_size, 5, 2, seed_vol)
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
                bids=bids,
                asks=asks,
                cond_vol=(vol ** 2) / YEAR_SECONDS,
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
                    }
                )
                prev = close
            return bars

    # ------------------------------------------------------------------ #
    # 单个 tick
    # ------------------------------------------------------------------ #
    def tick(self) -> dict[str, dict]:
        """推进一个时间步，返回最新行情快照表。

        交易模式约束：只有当前时段允许连续撮合（`is_open`）时才推进价格。
        集合竞价、午休、收盘后保持价格不变 —— 否则 K 线在休市时段还会继续长，
        和真实行情对不上，用户也会疑惑「收盘了怎么还在动」。
        引擎关闭（`engine_enabled=False`）时同样冻结，便于运营排查问题。
        """
        with self._lock:
            now = time.time()
            if now - self._last_tick_ts < settings.tick_seconds * 0.5:
                return {c: s.quote() for c, s in self._states.items()}
            self._last_tick_ts = now

            # 交易模式门禁：休市/集合竞价期间只回报最新快照，不推进
            session = current_session(now)
            self._session = session
            if not session.is_open:
                self.ready = self.ready or bool(self._states)
                return {c: s.quote() for c, s in self._states.items()}

            self._tick_count += 1
            self._segment = session.segment
            dt = settings.tick_seconds / YEAR_SECONDS
            sqdt = math.sqrt(dt)
            rng = self._rng

            # ---- 市场因子：OU 均值回复 + 波动率聚集 ----
            m_sigma_daily = 0.011
            m = -0.08 * self._market_ret + m_sigma_daily * math.sqrt(252) * sqdt * rng.gauss(0, 1)
            if rng.random() < 0.0006:
                m += rng.choice([-1, 1]) * rng.uniform(0.004, 0.012)
            self._market_ret = m

            for st in self._states.values():
                # 波动率聚集（GARCH 式递推）
                st.cond_vol = 0.90 * st.cond_vol + 0.10 * (m ** 2) * 40 + 1e-9
                inst_vol = math.sqrt(max(st.cond_vol, 1e-10)) * st.vol * 3.0
                idio = inst_vol * sqdt * rng.gauss(0, 1)
                ret = st.beta * m + st.drift * dt + idio
                if rng.random() < 0.0004:
                    ret += rng.choice([-1, 1]) * rng.uniform(0.01, 0.045)

                new_price = snap(st.price * math.exp(ret), st.tick_size)
                new_price = clip(new_price, st.limit_down, st.limit_up)
                st.price = new_price
                st.high = max(st.high, new_price)
                st.low = min(st.low, new_price)

                # ---- 量价关系 + U 型日内曲线 ----
                progress = (self._tick_count % 14400) / 14400
                st.day_progress = progress
                u_shape = 1.0 + 1.6 * (math.exp(-((progress - 0.02) ** 2) / 0.004) + math.exp(-((0.98 - progress) ** 2) / 0.004))
                intensity = (0.6 + 12 * abs(ret) * 40) * st.liquidity * u_shape
                traded = int(max(100, st.day_volume_base * intensity / math.sqrt(14400 / max(settings.tick_seconds, 0.5))))
                traded = traded // 100 * 100
                st.volume += traded
                st.amount += traded * new_price

                # ---- 盘口 ----
                spread_ticks = 1 + int(min(6, abs(ret) * 900)) + (1 if st.price >= st.limit_up - 1e-9 else 0)
                seed_vol = max(100.0, st.day_volume_base * 0.02 * st.liquidity)
                st.bids, st.asks = build_levels(st.price, st.tick_size, 5, spread_ticks, seed_vol)

                # ---- 分钟线聚合（带上时段标识，供客户端区分盘前/竞价/盘后） ----
                minute_ts = int(now // 60 * 60)
                if st.cur_minute is None or st.cur_minute.ts != minute_ts:
                    if st.cur_minute is not None:
                        st.minute_bars.append(st.cur_minute)
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

    def minute_series(self, code: str, limit: int = 240) -> list[dict]:
        with self._lock:
            st = self._states.get(code)
            if st is None:
                return []
            bars = list(st.minute_bars)
            if st.cur_minute is not None:
                bars.append(st.cur_minute)
            bars = bars[-limit:]
            return [
                {
                    "t": b.ts,
                    "price": b.close,
                    "volume": b.volume,
                    "avg": round(b.amount / b.volume, 4) if b.volume else b.close,
                    "segment": b.segment,
                }
                for b in bars
            ]

    def kline(self, code: str, period: str = "1min") -> list[dict]:
        """分钟级 K 线（1/5/15/30/60分钟），由分钟序列聚合。"""
        with self._lock:
            st = self._states.get(code)
            if st is None:
                return []
            bars = list(st.minute_bars)
            if st.cur_minute is not None:
                bars.append(st.cur_minute)
        step = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}.get(period, 1)
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
        with self._lock:
            change = round(self._index_value - self._index_prev_close, 2)
            return {
                "code": "SK000001",
                "name": "股岛综合指数",
                "price": self._index_value,
                "prev_close": self._index_prev_close,
                "open": self._index_open,
                "high": self._index_high,
                "low": self._index_low,
                "change": change,
                "change_pct": round(change / self._index_prev_close * 100, 2) if self._index_prev_close else 0.0,
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
        """引擎运行态快照：开关、节奏、交易模式、时段与标的统计。"""
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
                "session": {
                    "mode": st.mode,
                    "mode_label": st.mode_label,
                    "phase": st.phase,
                    "segment": st.segment,
                    "label": st.label,
                    "is_open": st.is_open,
                    "can_trade": st.can_trade,
                    "trade_date": st.trade_date,
                    "seconds_left": st.seconds_left,
                },
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
