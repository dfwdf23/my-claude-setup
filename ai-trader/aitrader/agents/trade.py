"""비중조절 · 매수 · 매도 에이전트."""
from __future__ import annotations

from ..config import FeeCfg, RiskCfg, SessionCfg
from ..indicators import round_tick, tick_size
from ..models import BUY, SELL, Features, OrderIntent, Position, Signal
from ..params import StrategyParams
from .base import Agent


class PositionSizer(Agent):
    """비중조절 — '손절 시 잃는 금액'이 자산의 일정 비율이 되도록 수량을 정한다.

    손절폭(1R)이 왕복 거래비용의 min_stop_cost_mult 배보다 작으면 그만큼 넓힌다.
    비용보다 작은 폭을 노리는 거래는 이겨도 남는 게 없기 때문이다.
    """
    name = "sizer"
    role = "비중조절"

    def __init__(self, cfg: RiskCfg, fees: FeeCfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.fees = fees

    def round_trip_cost(self, price: float, etf: bool, tax: float) -> float:
        """1주를 사고 팔 때 드는 비용(수수료 + 세금 + 호가 스프레드·슬리피지)."""
        return (price * (2 * self.fees.commission + tax)
                + (2 * self.fees.slippage_ticks + 1) * tick_size(price, etf))

    def size(self, price: float, atr: float, score: float, equity: float, cash: float,
             exposure: float, risk_mult: float, p: StrategyParams, etf: bool = False,
             tax: float = 0.0) -> tuple[int, float]:
        """(수량, 손절폭)을 돌려준다. 수량 0이면 매수하지 않는다."""
        self.calls += 1
        c = self.cfg
        stop_dist = max(p.stop_atr_mult * atr, c.min_stop_cost_mult * self.round_trip_cost(price, etf, tax))
        # 신호가 기준을 간신히 넘으면 절반, 강할수록 최대 1배
        span = 1.0 - p.entry_score_min
        conviction = 0.5 + 0.5 * min(1.0, (score - p.entry_score_min) / span) if span > 0 else 1.0
        risk_amt = equity * c.risk_per_trade * risk_mult * p.size_mult * conviction
        qty = int(risk_amt / stop_dist)
        qty = min(qty,
                  int(equity * c.max_weight / price),
                  int(max(0.0, equity * c.max_exposure - exposure) / price),
                  int(max(0.0, cash - equity * c.cash_buffer) / (price * (1 + self.fees.commission))))
        return (qty if qty > 0 else 0), stop_dist


class BuyAgent(Agent):
    """매수 — 체결 가능성이 높은 '시장성 지정가'(매도1호가 + n틱)로 주문을 만든다.

    순수 시장가보다 슬리피지 상한이 있고, 일반 지정가보다 즉시 체결 확률이 높다.
    """
    name = "buyer"
    role = "매수"

    def __init__(self, fees: FeeCfg) -> None:
        super().__init__()
        self.slip = fees.slippage_ticks

    def build(self, sig: Signal, f: Features, last: float, ask: float, qty: int,
              stop_dist: float, day_gain_pct: float, tod: float, regime_bias: str,
              etf: bool = False) -> OrderIntent:
        self.calls += 1
        ref = ask if ask > 0 else last
        limit = round_tick(ref + self.slip * tick_size(ref, etf), True, etf)
        ctx = {"score": round(sig.score, 3), "stop_dist": stop_dist, "atr": f.atr,
               "rsi": round(f.rsi, 1), "ext_atr": round((f.close - f.ema_fast) / f.atr, 2) if f.atr else 0.0,
               "trend": sig.parts["trend"], "vol_ratio": round(f.vol_ratio, 2),
               "above_vwap": sig.parts["vwap"], "breakout": sig.parts["brk"],
               "day_gain": round(day_gain_pct, 2), "tod": int(tod), "regime": regime_bias,
               "signal_px": last}
        return OrderIntent(sig.symbol, BUY, qty, limit, "entry", ctx)


class SellAgent(Agent):
    """매도 — 손절·익절·추적손절은 '틱마다' 검사(가장 빠른 경로), 추세이탈·시간청산은 봉마다."""
    name = "seller"
    role = "매도"

    def __init__(self, session: SessionCfg, fees: FeeCfg) -> None:
        super().__init__()
        self.flatten_tod = session.tod("flatten") if session.intraday_only else 10 ** 9
        self.slip = fees.slippage_ticks

    def on_tick(self, pos: Position, px: float, bid: float, tod: float,
                flatten_all: bool, etf: bool = False) -> OrderIntent | None:
        self.calls += 1
        if px > pos.high:
            pos.high = px
            if pos.trail_on or px >= pos.trail_trigger:
                pos.trail_on = True
                ns = px - pos.trail_dist
                if ns > pos.stop:
                    pos.stop = ns
                    pos.stop_kind = "trailing_stop"
        elif px < pos.low:
            pos.low = px
        if px <= pos.stop:
            return OrderIntent(pos.symbol, SELL, pos.qty, 0.0, pos.stop_kind)     # 시장가: 손절은 체결이 우선
        if px >= pos.target:
            return self._limit(pos, px, bid, "take_profit", etf)
        if flatten_all:
            return OrderIntent(pos.symbol, SELL, pos.qty, 0.0, "risk_flatten")
        if tod >= self.flatten_tod:
            return OrderIntent(pos.symbol, SELL, pos.qty, 0.0, "eod_flatten")
        return None

    def on_bar(self, pos: Position, f: Features, bid: float, p: StrategyParams,
               etf: bool = False) -> OrderIntent | None:
        self.calls += 1
        px = f.close
        if f.ready and px < f.ema_slow and f.ema_fast < f.ema_slow:
            return self._limit(pos, px, bid, "trend_exit", etf)
        if pos.bars_held >= p.time_stop_bars and px < pos.avg_price + 0.5 * pos.risk_per_share:
            return self._limit(pos, px, bid, "time_stop", etf)
        return None

    def _limit(self, pos: Position, px: float, bid: float, reason: str, etf: bool) -> OrderIntent:
        ref = bid if bid > 0 else px
        tk = tick_size(ref, etf)
        limit = round_tick(max(ref - self.slip * tk, tk), False, etf)
        return OrderIntent(pos.symbol, SELL, pos.qty, limit, reason)
