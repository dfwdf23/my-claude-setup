"""매매 엔진 — 빠른 경로 전체가 여기서 '동기 함수 호출'로 이어진다.

    틱 → (매도 검사) → 봉 갱신 → 차트분석 → 과매수 방지 → 비중조절 → 매수 → 리스크 → 주문

에이전트 사이에 큐·네트워크·LLM 호출이 하나도 없다. 그래서 틱 수신부터 주문 결정까지
수~수십 마이크로초에 끝난다. 실매매·모의·백테스트가 전부 이 같은 코드를 쓴다.
"""
from __future__ import annotations

from collections import deque
from time import perf_counter_ns
from typing import Callable

from .agents.chart import ChartAnalyst
from .agents.guards import OverboughtGuard, RiskManager
from .agents.trade import BuyAgent, PositionSizer, SellAgent
from .config import Config
from .indicators import IndicatorSet
from .models import BUY, Bar, Features, Fill, OrderIntent, Position, Regime, Tick, Trade
from .params import StrategyParams

KST = 32400.0
DAY = 86400.0


class LatencyStats:
    __slots__ = ("buf", "i", "n", "size")

    def __init__(self, size: int = 4096) -> None:
        self.size = size
        self.buf = [0] * size
        self.i = 0
        self.n = 0

    def add(self, v: int) -> None:
        self.buf[self.i] = v
        self.i = (self.i + 1) % self.size
        self.n += 1

    def summary(self, scale: float = 1.0) -> dict:
        m = min(self.n, self.size)
        if m == 0:
            return {"n": 0, "p50": 0.0, "p99": 0.0, "max": 0.0}
        s = sorted(self.buf[:m])
        return {"n": self.n, "p50": round(s[m // 2] / scale, 2),
                "p99": round(s[min(m - 1, int(m * 0.99))] / scale, 2), "max": round(s[-1] / scale, 2)}


class SymState:
    __slots__ = ("symbol", "last", "bid", "ask", "ind", "f", "bar_start", "o", "h", "l", "c", "v",
                 "day_open", "bar_count", "last_loss_bar", "retry_after", "score", "etf", "tax")

    def __init__(self, symbol: str, cfg, etf: bool = False, tax: float = 0.0) -> None:
        self.symbol = symbol
        self.etf = etf            # ETF: 호가단위 5원, 매도 거래세 없음
        self.tax = tax
        self.last = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.ind = IndicatorSet(cfg)
        self.f: Features = self.ind.f
        self.bar_start = -1.0
        self.o = self.h = self.l = self.c = 0.0
        self.v = 0
        self.day_open = 0.0
        self.bar_count = 0
        self.last_loss_bar = -10 ** 9
        self.retry_after = 0.0
        self.score = 0.0


class Engine:
    def __init__(self, cfg: Config, params: StrategyParams, cash: float,
                 execute: Callable[[OrderIntent], None] | None = None, quiet: bool = False) -> None:
        self.cfg = cfg
        self.p = params
        self.cash = cash
        self.execute = execute or (lambda it: None)
        self.quiet = quiet
        self.bar_sec = float(cfg.strategy.bar_sec)
        self.commission = cfg.fees.commission
        self.sell_tax = cfg.fees.sell_tax
        self.etfs = frozenset(cfg.etfs)
        self.close_tod = cfg.session.tod("close")
        # 에이전트
        self.chart = ChartAnalyst()
        self.guard = OverboughtGuard()
        self.sizer = PositionSizer(cfg.risk, cfg.fees)
        self.buyer = BuyAgent(cfg.fees)
        self.seller = SellAgent(cfg.session, cfg.fees)
        self.risk = RiskManager(cfg.risk, cfg.session)
        self.agents = [self.chart, self.guard, self.sizer, self.buyer, self.seller, self.risk]
        self.regime = Regime()
        # 상태
        self.sym: dict[str, SymState] = {}
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, OrderIntent] = {}
        self.now = 0.0
        self.day = -1
        self.equity = cash
        self.exposure = 0.0
        self.day_start_equity = cash
        self.realized_day = 0.0
        self.realized_total = 0.0
        self.fees_total = 0.0
        self.trades: list[Trade] = []          # 누적(세션 내)
        self.trades_day: list[Trade] = []
        self.equity_series: list[tuple[float, float]] = []
        self._mark_ts = 0.0
        self._eq_pt_ts = 0.0
        self.ticks = 0
        self.lat = LatencyStats()
        self.events: deque = deque(maxlen=400)
        self._seq = 0
        # 외부 훅(저널·학습) — 없으면 None
        self.on_bar_cb: Callable[[Bar], None] | None = None
        self.on_trade_cb: Callable[[Trade], None] | None = None
        self.on_fill_cb: Callable[[Fill], None] | None = None
        self.on_day_cb: Callable[[int], None] | None = None
        self.risk.new_day(cash)

    # ------------------------------------------------------------------ 이벤트
    def emit(self, kind: str, symbol: str, text: str, **data) -> None:
        if self.quiet:
            return
        self._seq += 1
        self.events.append({"seq": self._seq, "ts": self.now, "kind": kind, "symbol": symbol,
                            "text": text, **data})

    def symbol_state(self, symbol: str) -> SymState:
        s = self.sym.get(symbol)
        if s is None:
            etf = symbol in self.etfs
            s = self.sym[symbol] = SymState(symbol, self.cfg.strategy, etf, 0.0 if etf else self.sell_tax)
        return s

    def set_params(self, p: StrategyParams) -> None:
        self.p = p                                # 참조 교체 한 번 — 핫패스는 잠금이 필요 없다

    # ------------------------------------------------------------------ 핫패스
    def on_tick(self, t: Tick) -> None:
        t0 = perf_counter_ns()
        ts = t.ts
        self.now = ts
        self.ticks += 1
        day = int((ts + KST) // DAY)
        if day != self.day:
            self._new_day(day)
        s = self.sym.get(t.symbol)
        if s is None:
            s = self.symbol_state(t.symbol)
        px = t.price
        s.last = px
        if t.bid:
            s.bid = t.bid
            s.ask = t.ask
        if s.day_open == 0.0:
            s.day_open = px
        tod = (ts + KST) % DAY

        # 1) 보유 종목이면 매도 조건부터(가장 급한 일)
        pos = self.positions.get(t.symbol)
        if pos is not None and t.symbol not in self.pending and ts >= s.retry_after:
            it = self.seller.on_tick(pos, px, s.bid, tod, self.risk.flatten_all, s.etf)
            if it is not None:
                self._send(it, s)

        # 2) 봉 갱신 → 마감되면 매수 판단
        b = ts - ts % self.bar_sec
        if b != s.bar_start:
            if s.bar_start >= 0.0:
                self._bar_close(s, Bar(s.symbol, s.bar_start, s.o, s.h, s.l, s.c, s.v), tod)
            s.bar_start = b
            s.o = s.h = s.l = s.c = px
            s.v = t.volume
        else:
            if px > s.h:
                s.h = px
            elif px < s.l:
                s.l = px
            s.c = px
            s.v += t.volume

        if ts - self._mark_ts >= 5.0:
            self.mark()
        self.lat.add(perf_counter_ns() - t0)

    def _bar_close(self, s: SymState, bar: Bar, tod: float) -> None:
        f = s.ind.update(bar)
        s.bar_count += 1
        if self.on_bar_cb is not None:
            self.on_bar_cb(bar)
        sym = s.symbol
        if sym in self.pending or self.now < s.retry_after:
            return
        p = self.p
        pos = self.positions.get(sym)
        if pos is not None:
            pos.bars_held += 1
            it = self.seller.on_bar(pos, f, s.bid, p, s.etf)
            if it is not None:
                self._send(it, s)
            return
        if not f.ready:
            return
        sig = self.chart.evaluate(sym, f, p)
        if sig is None:
            s.score = 0.0
            return
        s.score = sig.score
        day_gain = (f.close / s.day_open - 1.0) * 100.0 if s.day_open > 0 else 0.0
        why = self.guard.check(f, day_gain, s.bar_count - s.last_loss_bar, p)
        if why:
            self.guard.note(f"{sym} 차단: {why}", self.now)
            self.emit("block", sym, f"과매수 방지: {why}", score=round(sig.score, 2))
            return
        regime = self.regime
        qty, stop_dist = self.sizer.size(f.close, f.atr, sig.score, self.equity, self.cash,
                                         self.exposure, regime.risk_mult, p, s.etf, s.tax)
        if qty <= 0:
            return
        it = self.buyer.build(sig, f, s.last, s.ask, qty, stop_dist, day_gain, tod, regime.bias, s.etf)
        why = self.risk.pre_trade(it, s.last, self.now, tod, len(self.positions) + self._pending_buys(),
                                  self.exposure, self.equity, regime)
        if why:
            self.emit("block", sym, f"리스크: {why}", score=round(sig.score, 2))
            return
        self.chart.note(f"{sym} 매수신호 {sig.score:.2f}", self.now)
        self.sizer.note(f"{sym} {qty}주 (위험배수 {regime.risk_mult * p.size_mult:.2f})", self.now)
        self._send(it, s)

    def _pending_buys(self) -> int:
        return sum(1 for it in self.pending.values() if it.side == BUY)

    def _send(self, it: OrderIntent, s: SymState) -> None:
        if it.side != BUY:
            why = self.risk.pre_trade(it, s.last, self.now, 0.0, 0, 0.0, self.equity, self.regime)
            if why:
                self.emit("block", it.symbol, f"리스크: {why}")
                s.retry_after = self.now + 3.0
                return
        self.pending[it.symbol] = it
        self.risk.note_order(self.now)
        (self.buyer if it.side == BUY else self.seller).note(
            f"{it.symbol} {it.qty}주 {'시장가' if not it.limit_price else format(it.limit_price, ',.0f')}"
            f" ({it.reason})", self.now)
        self.execute(it)

    # ------------------------------------------------------------------ 체결 반영
    def on_fill(self, fl: Fill) -> Trade | None:
        notional = fl.qty * fl.price
        sym = fl.symbol
        pos = self.positions.get(sym)
        trade = None
        if fl.side == BUY:
            fee = notional * self.commission
            self.cash -= notional + fee
            self.fees_total += fee
            if pos is None:
                ctx = dict(fl.ctx)
                p = self.p
                sd = ctx.get("stop_dist") or fl.price * 0.02
                ctx["slip"] = fl.price - ctx.get("signal_px", fl.price)
                ctx["pv"] = p.version
                self.positions[sym] = Position(
                    sym, fl.qty, fl.price, notional + fee, fl.ts, stop=fl.price - sd,
                    target=fl.price + p.take_profit_r * sd, risk_per_share=sd,
                    trail_trigger=fl.price + p.trail_start_r * sd,
                    trail_dist=p.trail_atr_mult * (ctx.get("atr") or sd / 2), high=fl.price,
                    low=fl.price, ctx=ctx)
            else:
                pos.avg_price = (pos.avg_price * pos.qty + notional) / (pos.qty + fl.qty)
                pos.qty += fl.qty
                pos.cost += notional + fee
            self.emit("fill", sym, f"매수 체결 {fl.qty}주 @ {fl.price:,.0f}", side=BUY,
                      qty=fl.qty, price=fl.price)
        elif pos is not None:
            qty = min(fl.qty, pos.qty)
            fee = qty * fl.price * (self.commission + self.sym[sym].tax if sym in self.sym
                                    else self.commission + self.sell_tax)
            proceeds = qty * fl.price - fee
            cost_part = pos.cost * qty / pos.qty
            pnl = proceeds - cost_part
            self.cash += proceeds
            self.fees_total += fee
            pos.realized += pnl
            pos.cost -= cost_part
            pos.qty -= qty
            pos.exit_qty += qty
            pos.exit_notional += qty * fl.price
            self.realized_day += pnl
            self.realized_total += pnl
            self.emit("fill", sym, f"매도 체결 {qty}주 @ {fl.price:,.0f} ({fl.reason}) "
                      f"손익 {pnl:+,.0f}", side="SELL", qty=qty, price=fl.price, pnl=round(pnl))
            if pos.qty <= 0:
                trade = self._close(pos, fl)
        if self.on_fill_cb is not None:
            self.on_fill_cb(fl)
        self.mark()
        return trade

    def _close(self, pos: Position, fl: Fill) -> Trade:
        del self.positions[pos.symbol]
        r = pos.risk_per_share or 1.0
        tq = pos.exit_qty
        orig_cost = pos.avg_price * tq * (1 + self.commission)
        tr = Trade(pos.symbol, pos.entry_ts, fl.ts, tq, pos.avg_price, pos.exit_notional / tq,
                   pos.realized, pos.realized / orig_cost if orig_cost else 0.0,
                   pos.realized / (r * tq), fl.reason or "manual", pos.bars_held,
                   (pos.avg_price - pos.low) / r, (pos.high - pos.avg_price) / r,
                   pos.ctx.get("pv", self.p.version), pos.ctx)
        self.trades.append(tr)
        self.trades_day.append(tr)
        s = self.sym.get(pos.symbol)
        if s is not None and tr.pnl < 0:
            s.last_loss_bar = s.bar_count
        self.risk.on_trade(tr, self.now)
        if self.on_trade_cb is not None:
            self.on_trade_cb(tr)
        return tr

    def on_order_done(self, symbol: str, ok: bool, reason: str = "", rejected: bool = False) -> None:
        """주문이 끝났을 때(전량 체결·취소·거부) 호출 — 해당 종목의 잠금을 푼다."""
        self.pending.pop(symbol, None)
        if not ok:
            s = self.sym.get(symbol)
            if s is not None:
                s.retry_after = self.now + 3.0
            self.emit("order", symbol, f"주문 미체결/거부: {reason}")
            if rejected and self.risk.note_reject(self.now):
                self.emit("risk", "", self.risk.halt_reason)

    # ------------------------------------------------------------------ 평가·일자
    def mark(self) -> None:
        now = self.now
        self._mark_ts = now
        exp = 0.0
        sym = self.sym
        for p in self.positions.values():
            exp += p.qty * sym[p.symbol].last
        self.exposure = exp
        eq = self.cash + exp
        self.equity = eq
        msg = self.risk.check_limits(eq)
        if msg:
            self.risk.note(msg, now)
            self.emit("risk", "", msg)
        if now - self._eq_pt_ts >= 60.0:
            self._eq_pt_ts = now
            self.equity_series.append((now, eq))

    def _new_day(self, day: int) -> None:
        first = self.day < 0
        self.day = day
        for s in self.sym.values():
            s.ind.reset_session()
            s.day_open = 0.0
            s.bar_start = -1.0
        self.mark() if not first else None
        self.day_start_equity = self.equity
        self.realized_day = 0.0
        self.trades_day = []
        self.equity_series = []
        self._eq_pt_ts = 0.0
        self.risk.new_day(self.equity)
        if self.on_day_cb is not None and not first:
            self.on_day_cb(day)

    def flush_bars(self) -> None:
        """장 마감 시 진행 중인 마지막 봉을 닫는다(기록용)."""
        for s in self.sym.values():
            if s.bar_start >= 0.0:
                bar = Bar(s.symbol, s.bar_start, s.o, s.h, s.l, s.c, s.v)
                s.ind.update(bar)
                s.bar_count += 1
                if self.on_bar_cb is not None:
                    self.on_bar_cb(bar)
                s.bar_start = -1.0

    def unrealized(self) -> float:
        return sum(p.qty * self.sym[p.symbol].last - p.cost for p in self.positions.values())
