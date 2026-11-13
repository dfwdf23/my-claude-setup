"""백테스터 — 실매매와 '같은 엔진'에 과거 틱/봉을 흘려 넣는다.

복기 에이전트가 파라미터 변경안을 채택하기 전에 여기서 검증한다.

봉 재생의 가정(보수적으로):
  · 봉 안의 가격 경로는 항상 시가 → 저가 → 고가 → 종가. 한 봉 안에 손절가와 익절가가 모두 있으면
    손절이 먼저 맞은 것으로 본다(매수 전용 전략에 불리한 쪽).
  · 손절·익절은 '그 가격 수준'에서 체결된 것으로 본다(봉의 저가·고가에서 체결된 것으로 치지 않는다).
    단, 시가가 이미 그 수준을 넘어 시작했으면(갭) 시가에 체결.
"""
from __future__ import annotations

from typing import Iterable

from .config import Config
from .engine import Engine
from .market import KRX, Market
from .metrics import summarize
from .models import BUY, Bar, Fill, OrderIntent, Tick
from .params import StrategyParams


def sim_fill_price(side: str, limit: float, last: float, bid: float, ask: float,
                   slip_ticks: int, etf: bool = False, market: Market = KRX) -> float | None:
    """모의 체결가. 즉시 체결 불가능한 지정가는 None."""
    if side == BUY:
        ref = ask if ask > 0 else last
        px = ref + slip_ticks * market.tick(ref, etf)
        if limit:
            if limit < ref:
                return None
            px = min(px, limit)
        return round(px, 4)
    ref = bid if bid > 0 else last
    tk = market.tick(ref, etf)
    px = max(ref - slip_ticks * tk, tk)
    if limit:
        if limit > ref:
            return None
        px = max(px, limit)
    return round(px, 4)


def bars_to_ticks(bars: Iterable[Bar], bar_sec: float = 60.0) -> Iterable[Tick]:
    """봉 1개 → 틱 4개(시가·저가·고가·종가 순)."""
    q = bar_sec / 4.0
    for b in bars:
        v = b.volume // 4
        for i, px in enumerate((b.open, b.low, b.high, b.close)):
            yield Tick(b.symbol, b.ts + i * q, px, v if i < 3 else b.volume - 3 * v)


class Backtester:
    def __init__(self, cfg: Config, params: StrategyParams, cash: float | None = None) -> None:
        self.cfg = cfg
        self.m = cfg.mkt
        self.start = cash if cash is not None else cfg.capital
        self.engine = Engine(cfg, params, self.start, execute=self._exec, quiet=True)
        self.slip = cfg.fees.slippage_ticks
        self._bar_mode = False
        self._kind: dict[str, int] = {}        # 봉 재생 중: 종목별로 지금 틱이 봉의 몇 번째 점인지(0=시가)

    def _exec(self, it: OrderIntent) -> None:
        e = self.engine
        s = e.sym[it.symbol]
        px = None
        if self._bar_mode and it.side != BUY and self._kind.get(it.symbol, 0) != 0:
            pos = e.positions.get(it.symbol)
            level = 0.0
            if pos is not None:
                if it.reason in ("stop_loss", "trailing_stop"):
                    level = pos.stop
                elif it.reason == "take_profit":
                    level = pos.target
            if level > 0.0:                       # 봉 중간에 닿은 손절/익절 → 그 가격 수준에서 체결
                tk = self.m.tick(level, s.etf)
                px = max(self.m.round_tick(level, False, s.etf) - self.slip * tk, tk)
        if px is None:
            px = sim_fill_price(it.side, it.limit_price, s.last, s.bid, s.ask, self.slip, s.etf, self.m)
        if px is None:
            e.on_order_done(it.symbol, False, "미체결")
            return
        e.on_fill(Fill(it.symbol, it.side, it.qty, px, e.now, reason=it.reason, ctx=it.ctx))
        e.on_order_done(it.symbol, True)

    def run_ticks(self, ticks: Iterable[Tick]) -> "Backtester":
        self._bar_mode = False
        on_tick = self.engine.on_tick
        for t in ticks:
            on_tick(t)
        return self

    def run_bars(self, bars: Iterable[Bar]) -> "Backtester":
        """시간순으로 정렬된 봉(여러 종목 섞여 있어도 됨)."""
        self._bar_mode = True
        q = float(self.cfg.strategy.bar_sec) / 4.0
        on_tick = self.engine.on_tick
        kind = self._kind
        for b in bars:
            v = b.volume // 4
            sym = b.symbol
            for i, px in enumerate((b.open, b.low, b.high, b.close)):
                kind[sym] = i
                on_tick(Tick(sym, b.ts + i * q, px, v if i < 3 else b.volume - 3 * v))
        self._bar_mode = False
        return self

    def result(self) -> dict:
        e = self.engine
        e.mark()
        m = summarize(e.trades, self.start)
        m["equity"] = round(e.equity, 2)
        m["open_positions"] = len(e.positions)
        return m
