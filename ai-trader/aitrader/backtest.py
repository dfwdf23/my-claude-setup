"""백테스터 — 실매매와 '같은 엔진'에 과거 틱/봉을 흘려 넣는다.

복기 에이전트가 파라미터 변경안을 채택하기 전에 여기서 검증한다.

봉 재생의 가정(매수 전용 전략에 불리한 쪽으로):
  · 봉 안의 가격 경로는 시가 → 저가 → 고가 → 저가 → 종가. 한 봉 안에 손절가와 익절가가 모두 있으면
    손절이 먼저 맞은 것으로 보고, 봉 안에서 고가를 따라 올라간 추적 손절가도 그 뒤의 저가로 다시 시험한다.
  · 손절·익절은 '그 가격 수준'에서 체결된 것으로 본다(봉의 저가·고가에서 체결된 것으로 치지 않는다).
    단, 시가가 이미 그 수준을 넘어 시작했으면(갭) 시가에 체결.
  · 일봉처럼 긴 봉은 전일 봉에 대한 판단을 '다음 날 시가'에 체결한다(그날의 고가·저가를 알기 전).
그래도 실제보다 좋게 나올 수 있다: 주문은 모두 체결된 것으로 치고, 호가 잔량·체결 지연은 반영하지 않는다.
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

    def run_bars(self, bars: Iterable[Bar], daily_ctx: bool = False,
                 daily_seed: dict[str, list[Bar]] | None = None) -> "Backtester":
        """시간순으로 정렬된 봉(여러 종목·여러 날이 섞여 있어도 됨).

        날이 바뀔 때마다 그날의 마지막 봉을 닫는다(실매매에서 장 마감 때 하는 일과 같다).
        daily_ctx: 날이 바뀔 때마다 '그 전날까지'의 일봉으로 일봉 맥락(전일 종가·고가, 평균 거래량, 일봉 추세)을
          채운다 — 실매매에서 총괄이 장전 준비 때 하는 일. 분봉을 재생할 때는 하루치 분봉을 일봉 하나로 묶어 쓴다.
        daily_seed: 재생 구간 이전의 일봉(종목별). 재생 첫날부터 일봉 맥락이 있게 한다.
        """
        self._bar_mode = True
        e = self.engine
        bar_sec = float(self.cfg.strategy.bar_sec)
        if bar_sec >= 3600.0:
            # 긴 봉(일봉 등)은 '다음 봉의 시가'에 판단·체결한 것으로 본다. 실매매는 매수 시작 시각(예: 09:35)의
            # 가격에 사지만, 재생에서 그 시각을 그대로 쓰면 판단이 봉의 둘째 점(저가)에서 일어나
            # '그날 저가에 산' 결과가 된다(미래 참조).
            e.decide_tod = float(e.open_tod)
            e.risk.entry_start = e.open_tod
        # 봉 안의 다섯 점을 봉 시작 직후에 몰아 둔다(긴 봉·반쪽짜리 마지막 봉도 장 시간 안에 들어오게)
        q = min(bar_sec / 5.0, 360.0)
        on_tick = e.on_tick
        kind = self._kind
        offset = self.m.utc_offset
        bars = bars if isinstance(bars, list) else list(bars)
        past: dict[str, list[Bar]] = {}
        if daily_ctx and daily_seed and bars:
            d0 = int((bars[0].ts + offset(bars[0].ts)) // 86400.0)
            for sym, hist in daily_seed.items():
                past[sym] = [b for b in hist if int((b.ts + offset(b.ts)) // 86400.0) < d0]
        today: dict[str, list] = {}               # 종목 → [시각, 시가, 고가, 저가, 종가, 거래량] (오늘치 묶음)
        day = None
        for b in bars:
            d = int((b.ts + offset(b.ts)) // 86400.0)
            if d != day:
                if day is not None:
                    e.flush_bars()
                if daily_ctx:
                    for sym, a in today.items():
                        past.setdefault(sym, []).append(Bar(sym, a[0], a[1], a[2], a[3], a[4], a[5]))
                    today = {}
                    for sym, hist in past.items():
                        e.set_daily(sym, hist[-60:])
                day = d
            sym = b.symbol
            if daily_ctx:
                a = today.get(sym)
                if a is None:
                    today[sym] = [b.ts, b.open, b.high, b.low, b.close, b.volume]
                else:
                    if b.high > a[2]:
                        a[2] = b.high
                    if b.low < a[3]:
                        a[3] = b.low
                    a[4] = b.close
                    a[5] += b.volume
            v = b.volume // 4
            # 시가 → 저가 → 고가 → 저가 → 종가. 저가를 고가 '앞뒤로' 모두 지나게 해, 고정 손절(고가 전의 저가)과
            # 봉 안에서 끌어올려진 추적 손절(고가 뒤의 저가) 양쪽에 불리한 순서로 본다.
            for i, (px, vol) in enumerate(((b.open, v), (b.low, v), (b.high, v), (b.low, 0), (b.close, b.volume - 3 * v))):
                kind[sym] = i
                on_tick(Tick(sym, b.ts + i * q, px, vol))
        e.flush_bars()
        self._bar_mode = False
        return self

    def result(self) -> dict:
        e = self.engine
        e.mark()
        m = summarize(e.trades, self.start)
        m["equity"] = round(e.equity, 2)
        m["open_positions"] = len(e.positions)
        return m
