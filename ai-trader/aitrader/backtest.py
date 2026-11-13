"""백테스터 — 실매매와 '같은 엔진'에 과거 틱/봉을 흘려 넣는다.

복기 에이전트가 파라미터 변경안을 채택하기 전에 여기서 검증한다.
"""
from __future__ import annotations

from typing import Iterable

from .config import Config
from .engine import Engine
from .indicators import tick_size
from .metrics import summarize
from .models import BUY, Bar, Fill, OrderIntent, Tick
from .params import StrategyParams


def sim_fill_price(side: str, limit: float, last: float, bid: float, ask: float,
                   slip_ticks: int, etf: bool = False) -> float | None:
    """모의 체결가. 즉시 체결 불가능한 지정가는 None."""
    if side == BUY:
        ref = ask if ask > 0 else last
        px = ref + slip_ticks * tick_size(ref, etf)
        if limit:
            if limit < ref:
                return None
            px = min(px, limit)
        return px
    ref = bid if bid > 0 else last
    px = max(ref - slip_ticks * tick_size(ref, etf), tick_size(ref, etf))
    if limit:
        if limit > ref:
            return None
        px = max(px, limit)
    return px


def bars_to_ticks(bars: Iterable[Bar], bar_sec: float = 60.0) -> Iterable[Tick]:
    """봉 1개 → 틱 4개(시·저·고·종 또는 시·고·저·종). 음봉은 고가를 먼저 찍었다고 가정."""
    q = bar_sec / 4.0
    for b in bars:
        path = (b.open, b.low, b.high, b.close) if b.close >= b.open else (b.open, b.high, b.low, b.close)
        v = b.volume // 4
        for i, px in enumerate(path):
            yield Tick(b.symbol, b.ts + i * q, px, v if i < 3 else b.volume - 3 * v)


class Backtester:
    def __init__(self, cfg: Config, params: StrategyParams, cash: float | None = None) -> None:
        self.cfg = cfg
        self.start = cash if cash is not None else cfg.capital
        self.engine = Engine(cfg, params, self.start, execute=self._exec, quiet=True)
        self.slip = cfg.fees.slippage_ticks

    def _exec(self, it: OrderIntent) -> None:
        e = self.engine
        s = e.sym[it.symbol]
        px = sim_fill_price(it.side, it.limit_price, s.last, s.bid, s.ask, self.slip, s.etf)
        if px is None:
            e.on_order_done(it.symbol, False, "미체결")
            return
        e.on_fill(Fill(it.symbol, it.side, it.qty, px, e.now, reason=it.reason, ctx=it.ctx))
        e.on_order_done(it.symbol, True)

    def run_ticks(self, ticks: Iterable[Tick]) -> "Backtester":
        on_tick = self.engine.on_tick
        for t in ticks:
            on_tick(t)
        return self

    def run_bars(self, bars: Iterable[Bar]) -> "Backtester":
        """시간순으로 정렬된 봉(여러 종목 섞여 있어도 됨)."""
        return self.run_ticks(bars_to_ticks(bars, float(self.cfg.strategy.bar_sec)))

    def result(self) -> dict:
        e = self.engine
        e.mark()
        m = summarize(e.trades, self.start)
        m["equity"] = round(e.equity)
        m["open_positions"] = len(e.positions)
        return m
