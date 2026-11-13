"""증분(O(1)) 기술지표. 봉 하나가 마감될 때마다 과거 전체를 다시 계산하지 않는다.

pandas/numpy 없이 순수 파이썬 — 종목당 봉 1개 갱신에 수 마이크로초.
"""
from __future__ import annotations

from collections import deque
from math import sqrt

from .models import Bar, Features


class EMA:
    __slots__ = ("k", "value", "n", "period", "_sum")

    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value = 0.0
        self.n = 0
        self._sum = 0.0

    def update(self, x: float) -> float:
        self.n += 1
        if self.n <= self.period:          # 초기 구간은 단순평균으로 시드
            self._sum += x
            self.value = self._sum / self.n
        else:
            self.value += self.k * (x - self.value)
        return self.value


class RSI:
    """Wilder RSI."""
    __slots__ = ("period", "prev", "gain", "loss", "n", "value")

    def __init__(self, period: int = 14):
        self.period = period
        self.prev = 0.0
        self.gain = 0.0
        self.loss = 0.0
        self.n = 0
        self.value = 50.0

    def update(self, close: float) -> float:
        if self.n == 0:
            self.prev = close
            self.n = 1
            return self.value
        ch = close - self.prev
        self.prev = close
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        p = self.period
        if self.n <= p:
            self.gain += g / p
            self.loss += l / p
        else:
            self.gain = (self.gain * (p - 1) + g) / p
            self.loss = (self.loss * (p - 1) + l) / p
        self.n += 1
        if self.loss <= 1e-12:
            self.value = 100.0 if self.gain > 0 else 50.0
        else:
            rs = self.gain / self.loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder ATR."""
    __slots__ = ("period", "prev_close", "value", "n")

    def __init__(self, period: int = 14):
        self.period = period
        self.prev_close = 0.0
        self.value = 0.0
        self.n = 0

    def update(self, high: float, low: float, close: float) -> float:
        if self.n == 0:
            tr = high - low
        else:
            pc = self.prev_close
            tr = max(high - low, abs(high - pc), abs(low - pc))
        self.prev_close = close
        self.n += 1
        if self.n <= self.period:
            self.value += (tr - self.value) / self.n
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class RollingStats:
    """이동평균·표준편차를 누적합으로 O(1) 갱신."""
    __slots__ = ("period", "buf", "s", "s2")

    def __init__(self, period: int):
        self.period = period
        self.buf: deque = deque()
        self.s = 0.0
        self.s2 = 0.0

    def update(self, x: float) -> None:
        self.buf.append(x)
        self.s += x
        self.s2 += x * x
        if len(self.buf) > self.period:
            old = self.buf.popleft()
            self.s -= old
            self.s2 -= old * old

    @property
    def mean(self) -> float:
        n = len(self.buf)
        return self.s / n if n else 0.0

    @property
    def std(self) -> float:
        n = len(self.buf)
        if n < 2:
            return 0.0
        m = self.s / n
        var = self.s2 / n - m * m
        return sqrt(var) if var > 0 else 0.0


class IndicatorSet:
    """종목 하나의 지표 묶음. update(bar) → Features."""
    __slots__ = ("ema_fast", "ema_slow", "rsi", "atr", "bb", "bb_k", "vol", "highs",
                 "pv", "v", "warmup", "n", "f")

    def __init__(self, cfg):
        self.ema_fast = EMA(cfg.ema_fast)
        self.ema_slow = EMA(cfg.ema_slow)
        self.rsi = RSI(cfg.rsi)
        self.atr = ATR(cfg.atr)
        self.bb = RollingStats(cfg.bb)
        self.bb_k = cfg.bb_k
        self.vol = RollingStats(cfg.vol_avg)
        self.highs: deque = deque(maxlen=cfg.breakout)
        self.pv = 0.0
        self.v = 0.0
        self.warmup = cfg.warmup_bars
        self.n = 0
        self.f = Features()

    def reset_session(self) -> None:
        """장 시작 시 VWAP만 초기화(이동평균류는 전일에서 이어감)."""
        self.pv = 0.0
        self.v = 0.0

    def update(self, bar: Bar) -> Features:
        c = bar.close
        f = self.f
        f.close = c
        f.ema_fast = self.ema_fast.update(c)
        f.ema_slow = self.ema_slow.update(c)
        f.rsi = self.rsi.update(c)
        f.atr = self.atr.update(bar.high, bar.low, c)
        # 돌파 기준 고가·평균 거래량은 "현재 봉을 넣기 전" 값으로 비교해야 미래참조가 없다
        f.hh = max(self.highs) if self.highs else bar.high
        avg_vol = self.vol.mean
        f.vol_ratio = bar.volume / avg_vol if avg_vol > 0 else 1.0
        self.highs.append(bar.high)
        self.vol.update(bar.volume)
        self.bb.update(c)
        f.bb_mid = self.bb.mean
        f.bb_upper = f.bb_mid + self.bb_k * self.bb.std
        typical = (bar.high + bar.low + c) / 3.0
        self.pv += typical * bar.volume
        self.v += bar.volume
        f.vwap = self.pv / self.v if self.v > 0 else c
        self.n += 1
        f.ready = self.n >= self.warmup
        return f


def tick_size(price: float, etf: bool = False) -> int:
    """KRX 호가가격단위(2023년 개편 기준, 코스피·코스닥 공통). ETF는 2,000원 이상 5원."""
    if price < 2_000:
        return 1
    if etf:
        return 5
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_tick(price: float, up: bool, etf: bool = False) -> float:
    t = tick_size(price, etf)
    q = int(price // t)
    if up and q * t < price:
        q += 1
    return float(max(q, 1) * t)
