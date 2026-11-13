"""시뮬레이션 시장 + 모의 브로커.

실제 시세가 아니라 '추세 구간/횡보 구간이 번갈아 나오는 합성 시세'다.
목적은 시스템 전체(에이전트·주문·복기·대시보드)를 돈 한 푼 안 쓰고 끝까지 돌려 보는 것이며,
여기서의 손익은 실전 수익성을 전혀 보장하지 않는다.
"""
from __future__ import annotations

import asyncio
import random
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from math import exp, log, sqrt
from typing import Iterator

from ..backtest import sim_fill_price
from ..config import Config
from ..indicators import round_tick, tick_size
from ..models import BUY, AccountSnapshot, Bar, Order, Tick
from .base import Broker, BrokerError

KST_TZ = timezone(timedelta(hours=9))
SESSION_SEC = 23400.0   # 09:00~15:30


def kst_epoch(d: date, hh: int = 9, mm: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST_TZ).timestamp()


class _S:
    __slots__ = ("px", "sig_tick", "beta", "base_vol", "regime", "anchor", "sigma")


class SimMarket:
    def __init__(self, symbols: list[str], seed: int = 7, tick_sec: float = 5.0,
                 start: date = date(2026, 1, 5), etfs: frozenset = frozenset()) -> None:
        self.symbols = list(symbols)
        self.etfs = etfs
        self.rng = rng = random.Random(seed)
        self.tick_sec = tick_sec
        self.date = start
        self.n = int(SESSION_SEC / tick_sec)
        self.p_switch = tick_sec / (45 * 60.0)                 # 구간 평균 지속 45분
        self.trend_tick = 0.016 * tick_sec / 3600.0            # 추세 구간: 시간당 ±1.6%
        self.mkt_sig_tick = 0.007 * sqrt(tick_sec / SESSION_SEC)
        self.state: dict[str, _S] = {}
        for sym in self.symbols:
            st = _S()
            if sym in etfs:
                st.px = float(rng.choice([6_200, 9_800, 14_500, 21_000, 33_000, 47_000]))
            else:
                st.px = float(rng.choice([27_500, 46_000, 71_000, 128_000, 215_000]))
            st.sigma = rng.uniform(0.016, 0.030)               # 일간 변동성
            st.sig_tick = st.sigma * sqrt(tick_sec / SESSION_SEC)
            st.beta = rng.uniform(0.6, 1.3)
            st.base_vol = max(5.0, 4e7 / st.px / self.n * rng.uniform(5, 30))
            st.regime = 0
            st.anchor = st.px
            self.state[sym] = st

    def next_day(self) -> None:
        d = self.date + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        self.date = d
        for st in self.state.values():
            st.px *= exp(self.rng.gauss(0.0, 0.007))           # 시초 갭
            st.anchor = st.px
            st.regime = 0

    def day_ticks(self) -> Iterator[Tick]:
        rng = self.rng
        n = self.n
        open_ts = kst_epoch(self.date)
        mkt_drift = (-0.02 if rng.random() < 0.12 else rng.gauss(0.0, 0.005)) / n   # 가끔 급락일
        syms = self.symbols
        k = len(syms)
        states = [self.state[s] for s in syms]
        gauss, rand = rng.gauss, rng.random
        for i in range(n):
            base_ts = open_ts + i * self.tick_sec
            mret = mkt_drift + gauss(0.0, self.mkt_sig_tick)
            x = 2.0 * i / n - 1.0
            u = 0.6 + 1.6 * x * x                               # 장 초반·마감에 거래량 집중
            for j in range(k):
                st = states[j]
                if rand() < self.p_switch:
                    st.regime = rng.choices((1, -1, 0), (0.30, 0.25, 0.45))[0]
                    st.anchor = st.px
                mu = st.regime * self.trend_tick if st.regime else -0.02 * log(st.px / st.anchor)
                st.px *= exp(mu + st.beta * mret + st.sig_tick * gauss(0.0, 1.0))
                etf = syms[j] in self.etfs
                px = round_tick(st.px, False, etf)
                tk = tick_size(px, etf)
                vol = int(st.base_vol * u * (1.0 + 1.5 * abs(st.regime)) * rng.lognormvariate(0.0, 0.5)) + 1
                if rand() < 0.5:
                    bid, ask = px, px + tk
                else:
                    bid, ask = max(px - tk, tk), px
                yield Tick(syms[j], base_ts + j * self.tick_sec / k, px, vol, float(bid), float(ask))

    def history(self, symbol: str, n: int) -> list[Bar]:
        """종목선정용 합성 일봉(과거 n일)."""
        st = self.state[symbol]
        rng = random.Random(zlib.crc32(symbol.encode()))
        px = st.px
        out: list[Bar] = []
        d = self.date
        for _ in range(n):
            d -= timedelta(days=1)
            prev = px / exp(rng.gauss(0.0, st.sigma))
            hi, lo = max(px, prev) * (1 + st.sigma / 3), min(px, prev) * (1 - st.sigma / 3)
            out.append(Bar(symbol, kst_epoch(d), prev, hi, lo, px, int(st.base_vol * self.n)))
            px = prev
        out.reverse()
        return out


class PaperBroker(Broker):
    """합성 시세로 주문을 모의 체결하는 브로커(mode = sim)."""
    name = "paper"
    drives_clock = True
    has_account = False

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.etfs = frozenset(cfg.etfs)
        self.market = SimMarket(cfg.universe[:cfg.max_symbols], cfg.sim.seed, cfg.sim.tick_sec,
                                etfs=self.etfs)
        self.speed = max(cfg.sim.speed, 0.01)
        self.latency = cfg.sim.latency_ms / 1000.0
        self.slip = cfg.fees.slippage_ticks
        self.last: dict[str, Tick] = {}
        self.resting: dict[str, Order] = {}
        self.sim_now = 0.0
        self.days_done = 0
        self._n = 0
        self._stop = False

    def now(self) -> float:
        return self.sim_now or time.time()

    async def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        days = self.cfg.sim.days
        while not self._stop:
            self.sim_now = kst_epoch(self.market.date, 8, 40)
            await self.on_day_start(self.market.date)
            wall0 = loop.time()
            sim0 = kst_epoch(self.market.date)
            i = 0
            for t in self.market.day_ticks():
                if self._stop:
                    return
                delay = wall0 + (t.ts - sim0) / self.speed - loop.time()
                if delay > 0.001:
                    await asyncio.sleep(delay)
                elif i & 127 == 0:
                    await asyncio.sleep(0)             # 초고속 배속에서도 다른 작업에 양보
                i += 1
                self.sim_now = t.ts
                self.last[t.symbol] = t
                if self.resting:
                    self._check_resting(t)
                self.on_tick(t)
            self.sim_now = kst_epoch(self.market.date, 15, 40)
            await self.on_day_end(self.market.date)
            self.market.next_day()
            self.days_done += 1
            if days and self.days_done >= days:
                return

    def _check_resting(self, t: Tick) -> None:
        for bid_, o in list(self.resting.items()):
            if o.symbol != t.symbol:
                continue
            if (o.side == BUY and t.ask <= o.limit_price) or (o.side != BUY and t.bid >= o.limit_price):
                del self.resting[bid_]
                self.on_fill(bid_, o.qty, o.limit_price, t.ts)

    async def place_order(self, order: Order) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)
        t = self.last.get(order.symbol)
        if t is None:
            raise BrokerError("시세 없음")
        self._n += 1
        order.broker_id = f"P{self._n}"
        px = sim_fill_price(order.side, order.limit_price, t.price, t.bid, t.ask, self.slip,
                            order.symbol in self.etfs)
        if px is None:
            self.resting[order.broker_id] = order
        else:
            asyncio.get_running_loop().call_soon(self.on_fill, order.broker_id, order.qty, px, t.ts)

    async def cancel_order(self, order: Order) -> None:
        if self.resting.pop(order.broker_id, None) is None:
            raise BrokerError("이미 체결되었거나 없는 주문")

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(self.cfg.capital, {})

    async def get_daily_bars(self, symbol: str, n: int) -> list[Bar]:
        return self.market.history(symbol, n) if symbol in self.market.state else []
