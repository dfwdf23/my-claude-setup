"""매매 엔진 — 빠른 경로 전체가 여기서 '동기 함수 호출'로 이어진다.

    틱 → (매도 검사) → 봉 갱신 → 차트분석 → 과매수 방지 → 비중조절 → 매수 → 리스크 → 주문

에이전트 사이에 큐·네트워크·LLM 호출이 하나도 없다. 그래서 틱 수신부터 주문 결정까지
수~수십 마이크로초에 끝난다. 실매매·모의·백테스트가 전부 이 같은 코드를 쓴다.
시장(KRX/US)에 따라 달라지는 것은 cfg.mkt(시장 프로필)에서만 가져온다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from math import pi, sin
from time import perf_counter_ns
from typing import Callable

from .agents.chart import ChartAnalyst
from .agents.guards import OverboughtGuard, RiskManager
from .agents.trade import BuyAgent, PositionSizer, SellAgent
from .config import Config
from .indicators import IndicatorSet
from .models import BUY, Bar, Features, Fill, NewsState, OrderIntent, Position, Regime, Tick, Trade, reason_ko
from .params import StrategyParams

DAY = 86400.0
TWO_PI = 2.0 * pi


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
                 "day_open", "bar_count", "last_loss_bar", "retry_after", "score", "etf", "tax",
                 "cum_vol", "prev_close", "prev_high", "avg_dvol", "dtrend", "rvol", "hist", "undecided",
                 "v_start", "vo", "vh", "vl", "vc", "vv", "view")

    def __init__(self, symbol: str, cfg, etf: bool = False, tax: float = 0.0) -> None:
        self.symbol = symbol
        self.etf = etf            # 국내 ETF: 호가단위 5원, 매도 거래세 없음
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
        self.cum_vol = 0          # 당일 누적 거래량
        # 일봉 맥락(장전 준비 때 채움). 없으면 0 → 해당 요소는 중립으로 처리
        self.prev_close = 0.0
        self.prev_high = 0.0
        self.avg_dvol = 0.0       # 최근 20일 평균 일거래량
        self.dtrend = 0.5         # 일봉 추세: 1 상승 정배열, 0 하락 역배열, 0.5 중립/모름
        self.rvol = -1.0          # 상대 거래량(평소 같은 시각 대비). -1 = 모름
        self.hist: deque = deque(maxlen=400)   # 차트 표시용 최근 봉(지표 포함)
        self.undecided = False    # 장 마감 때 닫은 봉에 대한 매매 판단이 아직 안 됨(다음 장 매수 시작 시각에 판단)
        # 화면용 분봉(전략 봉이 일봉·시간봉일 때도 실시간 차트를 보여 주기 위한 별도 봉)
        self.v_start = -1.0
        self.vo = self.vh = self.vl = self.vc = 0.0
        self.vv = 0
        self.view: deque = deque(maxlen=420)


class Engine:
    def __init__(self, cfg: Config, params: StrategyParams, cash: float,
                 execute: Callable[[OrderIntent], None] | None = None, quiet: bool = False) -> None:
        self.cfg = cfg
        self.m = cfg.mkt
        self.p = params
        self.cash = cash
        self.execute = execute or (lambda it: None)
        self.quiet = quiet
        self.clock = None             # 이벤트 시각용 시계(총괄이 브로커 시계를 연결). 없으면 마지막 틱 시각
        self.retry_sec = 3.0          # 주문이 미체결·거부로 끝난 뒤 같은 종목을 다시 시도하기까지의 간격
        self.bar_sec = float(cfg.strategy.bar_sec)
        self.commission = cfg.fees.commission
        self.sell_tax = cfg.fees.sell_tax
        self.etfs = frozenset(cfg.etfs) if cfg.market == "KRX" else frozenset()
        self.open_tod = float(cfg.session.tod("open"))
        self.close_tod = float(cfg.session.tod("close"))
        self.session_len = max(self.close_tod - self.open_tod, 1.0)
        self.carry = not cfg.session.intraday_only     # 포지션을 다음 날로 넘기는 방식(스윙)
        self.decide_tod = float(cfg.session.tod("entry_start"))
        self.view_sec = float(cfg.strategy.view_sec)
        self.view_on = not quiet and self.bar_sec > self.view_sec
        self.daily = self.bar_sec >= DAY               # 일봉: 봉 시각 = 그날 장 시작(현지 날짜가 밀리지 않게)
        self.tz_off = 0.0             # (현지 − UTC) 초. 한 시간에 한 번만 다시 계산
        self._tz_until = 0.0
        # 에이전트
        self.chart = ChartAnalyst()
        self.guard = OverboughtGuard()
        self.sizer = PositionSizer(cfg.risk, cfg.fees, self.m)
        self.buyer = BuyAgent(cfg.fees, self.m)
        self.seller = SellAgent(cfg.session, cfg.fees, self.m)
        self.risk = RiskManager(cfg.risk, cfg.session)
        self.agents = [self.chart, self.guard, self.sizer, self.buyer, self.seller, self.risk]
        self.regime = Regime()
        self.news: dict[str, NewsState] = {}   # 정보수집 에이전트가 갱신(종목별 소식 상태)
        self.exit_now: set[str] = set()        # 심각한 악재로 즉시 청산할 종목
        self.bar_exit: dict[str, str] = {}     # 봉 기준 청산(추세 이탈·시간 청산)이 결정됐지만 아직 체결되지 않은 종목 → 사유
        # 상태
        self.sym: dict[str, SymState] = {}
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, OrderIntent] = {}
        self._resv: dict[str, float] = {}      # 진행 중 매수 주문이 잡아 둔 금액
        self.reserved = 0.0
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
        self.fill_seq = 0                      # 체결이 반영될 때마다 증가(계좌 대사의 '장부 버전')
        self.hook_errors = 0
        self.lat = LatencyStats()
        self.events: deque = deque(maxlen=400)
        self._seq = 0
        # 외부 훅(저널·상태 저장) — 없으면 None. 훅의 예외는 매매 경로를 끊지 않는다.
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
        ts = self.clock() if self.clock is not None else self.now      # 장 밖(시작·복기·대사)의 기록도 제 시각으로
        self.events.append({"seq": self._seq, "ts": ts, "kind": kind, "symbol": symbol,
                            "text": text, **data})

    def _hook(self, cb, arg) -> None:
        try:
            cb(arg)
        except Exception as e:                     # 일지·상태 저장 실패가 주문 처리를 막으면 안 된다
            self.hook_errors += 1
            if self.hook_errors <= 20:
                self.emit("system", "", f"기록 훅 오류(매매는 계속): {e!r}")

    def symbol_state(self, symbol: str) -> SymState:
        s = self.sym.get(symbol)
        if s is None:
            etf = symbol in self.etfs
            s = self.sym[symbol] = SymState(symbol, self.cfg.strategy, etf, 0.0 if etf else self.sell_tax)
        return s

    def set_params(self, p: StrategyParams) -> None:
        self.p = p                                # 참조 교체 한 번 — 핫패스는 잠금이 필요 없다

    def warm(self, symbol: str, bars: list[Bar]) -> int:
        """지표 예열: 과거 봉으로 지표와 차트 기록을 처음부터 다시 채운다(매매 판단은 하지 않는다).
        총괄(장전 준비)·백테스트·지연 측정이 모두 이 함수를 쓴다. 넣은 봉 수를 돌려준다."""
        s = self.symbol_state(symbol)
        s.ind = IndicatorSet(self.cfg.strategy)
        s.f = s.ind.f
        s.hist.clear()
        for b in bars:
            if self.daily:
                s.ind.reset_session()             # 일봉은 봉 하나가 하루(세션) — 매매·재생과 같은 기준
            f = s.ind.update(b)
            s.hist.append((b.ts, b.open, b.high, b.low, b.close, b.volume, f.ema_fast, f.ema_slow, f.vwap))
        s.ind.reset_session()
        return len(bars)

    def set_daily(self, symbol: str, bars: list[Bar]) -> None:
        """일봉 맥락(전일 종가·고가, 평균 거래량, 일봉 추세). bars 는 '어제까지'의 일봉, 오래된 순."""
        s = self.symbol_state(symbol)
        if len(bars) < 5:
            return
        closes = [b.close for b in bars]
        s.prev_close = closes[-1]
        s.prev_high = bars[-1].high
        vols = [b.volume for b in bars[-20:]]
        s.avg_dvol = sum(vols) / len(vols)
        ma20 = sum(closes[-20:]) / len(closes[-20:])
        ma50 = sum(closes[-50:]) / len(closes[-50:])
        c = closes[-1]
        s.dtrend = 1.0 if c > ma20 > ma50 else (0.0 if c < ma20 < ma50 else 0.5)

    def seed_bar(self, symbol: str, bar: Bar, now: float) -> None:
        """일봉 방식에서 장중에 켜졌을 때: 오늘 봉의 이미 지나간 구간(시가·고가·저가·거래량)을 채워 넣는다.
        이렇게 하지 않으면 오늘 봉이 '켠 뒤의 움직임'만 담아 변동폭(ATR)·거래량이 실제보다 작게 기록된다."""
        if now >= self._tz_until:
            self._refresh_tz(now)
        loc = now + self.tz_off
        if not self.open_tod <= loc % DAY < self.close_tod:
            return
        day = int(loc // DAY)
        if day < self.day:
            return
        if day != self.day:
            self._new_day(day)
        s = self.symbol_state(symbol)
        if s.bar_start >= 0.0 or bar.close <= 0:
            return                                 # 이미 틱으로 만들고 있는 봉은 건드리지 않는다
        s.bar_start = now - loc % DAY + self.open_tod if self.daily else now - now % self.bar_sec
        s.o, s.h, s.l, s.c, s.v = bar.open, bar.high, bar.low, bar.close, bar.volume
        s.day_open = bar.open
        s.cum_vol = bar.volume
        if not s.last:
            s.last = bar.close

    def _refresh_tz(self, ts: float) -> None:
        self.tz_off = self.m.utc_offset(ts)
        self._tz_until = ts + 3600.0

    # ------------------------------------------------------------------ 핫패스
    def on_tick(self, t: Tick) -> None:
        t0 = perf_counter_ns()
        ts = t.ts
        self.ticks += 1
        if ts >= self._tz_until:
            self._refresh_tz(ts)
        loc = ts + self.tz_off
        tod = loc % DAY
        if tod < self.open_tod or tod >= self.close_tod:
            return                                 # 정규장 밖의 틱은 지표·판단에 쓰지 않는다
        day = int(loc // DAY)
        if day < self.day:
            return                                 # 지난 날짜가 찍힌 시세(지연·정지 종목) — 날짜를 되돌리지 않고 버린다
        self.now = ts
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
        if t.cum_volume:
            s.cum_vol = t.cum_volume
        else:
            s.cum_vol += t.volume

        # 1) 보유 종목이면 매도 조건부터(가장 급한 일)
        pos = self.positions.get(t.symbol)
        if pos is not None and t.symbol not in self.pending and ts >= s.retry_after:
            # 봉 기준 청산이 결정됐는데 주문이 체결되지 않았으면(시간 초과·제안 만료) 체결될 때까지 다시 낸다.
            # 일봉 방식은 판단이 하루 한 번뿐이라, 그러지 않으면 청산이 다음 날로 밀린다.
            force = "risk_flatten" if self.risk.flatten_all else (
                "news_exit" if t.symbol in self.exit_now else self.bar_exit.get(t.symbol, ""))
            it = self.seller.on_tick(pos, px, s.bid, tod, force, s.etf)
            if it is not None:
                self._send(it, s)

        # 2) 봉 갱신 → 마감되면 매수 판단
        b = ts - tod + self.open_tod if self.daily else ts - ts % self.bar_sec
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

        if s.undecided and tod >= self.decide_tod:
            # 어제 마감 때 닫힌 봉(일봉 방식에서는 하루 전체)에 대한 판단을 오늘 매수 시작 시각에 한다.
            # 진행 중인 주문·재시도 대기 때문에 판단을 못 했으면 표시를 남겨 다음 틱에 다시 본다(하루치 판단을 잃지 않게).
            if not self.carry or self._decide(s, s.f, tod):
                s.undecided = False

        if self.view_on:
            vb = ts - ts % self.view_sec
            if vb != s.v_start:
                if s.v_start >= 0.0:
                    s.view.append((s.v_start, s.vo, s.vh, s.vl, s.vc, s.vv))
                s.v_start = vb
                s.vo = s.vh = s.vl = s.vc = px
                s.vv = t.volume
            else:
                if px > s.vh:
                    s.vh = px
                elif px < s.vl:
                    s.vl = px
                s.vc = px
                s.vv += t.volume

        if ts - self._mark_ts >= 5.0:
            self.mark()
        self.lat.add(perf_counter_ns() - t0)

    def _ingest(self, s: SymState, bar: Bar) -> Features:
        """마감된 봉을 지표·차트 기록·일지에 반영한다(매매 판단은 하지 않는다)."""
        f = s.ind.update(bar)
        s.bar_count += 1
        s.hist.append((bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume,
                       f.ema_fast, f.ema_slow, f.vwap))
        if self.on_bar_cb is not None:
            self._hook(self.on_bar_cb, bar)
        return f

    def _bar_close(self, s: SymState, bar: Bar, tod: float) -> None:
        self._decide(s, self._ingest(s, bar), tod)

    def _decide(self, s: SymState, f: Features, tod: float) -> bool:
        """봉 마감 시점의 매매 판단: 보유 중이면 봉 단위 청산 조건, 아니면 매수 신호.
        판단을 실제로 했으면 True, 진행 중인 주문·재시도 대기로 미뤘으면 False."""
        # 상대 거래량: 평소 이 시각까지 쌓였을 거래량 대비 오늘 누적 거래량(장 초·마감에 몰리는 U자 분포 반영)
        x = (tod - self.open_tod) / self.session_len
        if s.avg_dvol > 0.0 and x > 0.02:
            s.rvol = s.cum_vol / (s.avg_dvol * (x + 0.6 * sin(TWO_PI * x) / TWO_PI))
        else:
            s.rvol = -1.0
        sym = s.symbol
        if sym in self.pending or self.now < s.retry_after:
            return False
        p = self.p
        pos = self.positions.get(sym)
        if pos is not None:
            pos.bars_held += 1
            it = self.seller.on_bar(pos, f, s.bid, p, s.etf, s.last)
            if it is not None:
                self.bar_exit[sym] = it.reason       # 체결돼 포지션이 닫힐 때까지 유지(_close 에서 지운다)
                self._send(it, s)
            return True
        if not f.ready:
            return True
        if self.daily:
            # 일봉 방식: 판단 대상은 '방금 닫힌 일봉'이므로, 그 봉 기준의 전일 고가·종가는 한 봉 앞의 값이다.
            # (set_daily 가 주는 prev_high·prev_close 는 판단 대상 봉 자신의 값이라 여기에 쓰면 항상 0이 된다)
            pb = s.hist[-2] if len(s.hist) >= 2 else None
            prev_high = pb[2] if pb is not None else 0.0
            ref = pb[4] if pb is not None else 0.0
        else:
            prev_high = s.prev_high
            ref = s.prev_close or s.day_open
        prev_brk = 0.5 if prev_high <= 0.0 else (1.0 if f.close > prev_high else 0.0)
        sig = self.chart.evaluate(sym, f, p, s.rvol, s.dtrend, prev_brk)
        if sig is None:
            s.score = 0.0
            return True
        s.score = sig.score
        ns = self.news.get(sym)
        if (ns is not None and self.now < ns.block_until) or sym in self.exit_now:
            self.guard.blocks["악재 뉴스"] += 1
            self.emit("block", sym, f"정보수집: 악재 뉴스로 매수 보류 — {ns.headline[:60] if ns is not None else ''}",
                      score=round(sig.score, 2))
            return True
        day_gain = (f.close / ref - 1.0) * 100.0 if ref > 0 else 0.0
        why = self.guard.check(f, day_gain, s.bar_count - s.last_loss_bar, p)
        if why:
            self.guard.note(f"{sym} 차단: {why}", self.now)
            self.emit("block", sym, f"과매수 방지: {why}", score=round(sig.score, 2))
            return True
        regime = self.regime
        # 아직 체결되지 않은 매수 주문이 잡아 둔 금액도 이미 쓴 것으로 본다(동시 신호로 한도 초과 방지)
        exposure = self.exposure + self.reserved
        qty, stop_dist = self.sizer.size(f.close, f.atr, sig.score, self.equity, self.cash - self.reserved,
                                         exposure, regime.risk_mult, p, s.etf, s.tax)
        if qty <= 0:
            return True
        gap = (s.day_open / s.prev_close - 1.0) * 100.0 if s.prev_close > 0 and s.day_open > 0 else 0.0
        it = self.buyer.build(sig, f, s.last, s.ask, qty, stop_dist, day_gain, tod, regime.bias, s.etf,
                              {"rvol": round(s.rvol, 2), "gap": round(gap, 2), "dtrend": s.dtrend,
                               "news": round(ns.score, 2) if ns is not None else 0.0})
        why = self.risk.pre_trade(it, s.last, self.now, tod, len(self.positions) + self._pending_buys(),
                                  exposure, self.equity, regime)
        if why:
            self.emit("block", sym, f"리스크: {why}", score=round(sig.score, 2))
            return True
        self.chart.note(f"{sym} 매수신호 {sig.score:.2f}", self.now)
        self.sizer.note(f"{sym} {qty}주 (위험배수 {regime.risk_mult * p.size_mult:.2f})", self.now)
        self._send(it, s)
        return True

    def _pending_buys(self) -> int:
        return sum(1 for it in self.pending.values() if it.side == BUY)

    def _send(self, it: OrderIntent, s: SymState) -> None:
        if it.side != BUY:
            why = self.risk.pre_trade(it, s.last, self.now, 0.0, 0, 0.0, self.equity, self.regime)
            if why:
                self.emit("block", it.symbol, f"리스크: {why}")
                s.retry_after = self.now + 3.0
                return
        else:
            amt = it.qty * (it.limit_price or s.last)
            self._resv[it.symbol] = amt
            self.reserved += amt
        self.pending[it.symbol] = it
        self.risk.note_order(self.now)
        (self.buyer if it.side == BUY else self.seller).note(
            f"{it.symbol} {it.qty}주 {'시장가' if not it.limit_price else self.m.px(it.limit_price)}"
            f" ({reason_ko(it.reason)})", self.now)
        self.execute(it)

    # ------------------------------------------------------------------ 체결 반영
    def on_fill(self, fl: Fill) -> Trade | None:
        notional = fl.qty * fl.price
        sym = fl.symbol
        pos = self.positions.get(sym)
        trade = None
        self.fill_seq += 1
        st = self.symbol_state(sym)                 # 시세를 한 번도 못 받은 종목의 체결(재시작 직후 등)도 평가 가능하게
        if st.last == 0.0:
            st.last = fl.price
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
            self.emit("fill", sym, f"매수 체결 {fl.qty}주 @ {self.m.px(fl.price)}", side=BUY,
                      qty=fl.qty, price=fl.price)
        elif pos is not None:
            qty = min(fl.qty, pos.qty)
            s = self.sym.get(sym)
            fee = qty * fl.price * (self.commission + (s.tax if s is not None else self.sell_tax))
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
            self.emit("fill", sym, f"매도 체결 {qty}주 @ {self.m.px(fl.price)} ({reason_ko(fl.reason)}) "
                      f"손익 {self.m.money(pnl)}", side="SELL", qty=qty, price=fl.price, pnl=round(pnl, 2))
            if pos.qty <= 0:
                trade = self._close(pos, fl)
        if self.on_fill_cb is not None:
            self._hook(self.on_fill_cb, fl)
        self.mark()
        return trade

    def _close(self, pos: Position, fl: Fill) -> Trade:
        del self.positions[pos.symbol]
        self.exit_now.discard(pos.symbol)
        self.bar_exit.pop(pos.symbol, None)
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
            self._hook(self.on_trade_cb, tr)
        return tr

    def on_order_done(self, symbol: str, ok: bool, reason: str = "", rejected: bool = False) -> None:
        """주문이 끝났을 때(전량 체결·취소·거부) 호출 — 해당 종목의 잠금을 푼다."""
        self.pending.pop(symbol, None)
        self.reserved -= self._resv.pop(symbol, 0.0)
        if not self._resv:
            self.reserved = 0.0
        if not ok:
            s = self.sym.get(symbol)
            if s is not None:
                s.retry_after = self.now + self.retry_sec
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
            if s.bar_start >= 0.0:
                # 장 마감 처리를 못 하고 날이 바뀌었다(절전·중단 등) → 어제의 마지막 봉을 지금 닫아 지표에 반영한다
                self._ingest(s, Bar(s.symbol, s.bar_start, s.o, s.h, s.l, s.c, s.v))
                s.undecided = True
            s.ind.reset_session()
            s.day_open = 0.0
            s.bar_start = -1.0
            s.cum_vol = 0
            s.rvol = -1.0
        # 장 마감 뒤에 확인된 심각한 악재의 청산 지시는 다음 장에서 실행돼야 한다 → 보유 중인 종목의 지시는 남긴다
        self.exit_now.intersection_update(self.positions)
        if not first:
            self.mark()
        self.day_start_equity = self.equity
        self.realized_day = 0.0
        self.trades_day = []
        self.equity_series = []
        self._eq_pt_ts = 0.0
        self.risk.new_day(self.equity)
        if self.on_day_cb is not None and not first:
            self._hook(self.on_day_cb, day)

    def flush_bars(self) -> None:
        """장 마감 시 진행 중인 마지막 봉을 닫는다(기록용)."""
        for s in self.sym.values():
            if s.bar_start >= 0.0:
                self._ingest(s, Bar(s.symbol, s.bar_start, s.o, s.h, s.l, s.c, s.v))
                s.undecided = True
                s.bar_start = -1.0

    def unrealized(self) -> float:
        return sum(p.qty * self.sym[p.symbol].last - p.cost for p in self.positions.values())

    def rvol_now(self, s: SymState) -> float:
        """화면 표시용 실시간 상대 거래량. 매매 판단에는 봉 마감 때 계산한 s.rvol 을 쓴다(빠른 경로 밖에서만 호출)."""
        x = min(((self.now + self.tz_off) % DAY - self.open_tod) / self.session_len, 1.0)
        if s.avg_dvol > 0.0 and s.cum_vol > 0 and x > 0.02:
            return s.cum_vol / (s.avg_dvol * (x + 0.6 * sin(TWO_PI * x) / TWO_PI))
        return -1.0

    # ------------------------------------------------------------------ 재시작 대비 상태
    def export_state(self) -> dict:
        sym = self.sym
        # 손실 청산 뒤 재진입 금지(봉 수 기준)가 재시작으로 풀리지 않게, 종목별 '손실 후 지난 봉 수'를 남긴다
        since = {k: s.bar_count - s.last_loss_bar for k, s in sym.items() if s.bar_count - s.last_loss_bar < 1000}
        return {"cash": self.cash, "realized_total": self.realized_total, "fees_total": self.fees_total,
                "day": self.day, "day_start_equity": self.day_start_equity, "realized_day": self.realized_day,
                "positions": [asdict(p) for p in self.positions.values()], "risk": self.risk.export(),
                # 보유 종목의 마지막 가격 — 재시작 직후 취득가로 평가하면 평가손익이 통째로 사라져 한도가 잘못 발동한다
                "last": {p.symbol: sym[p.symbol].last for p in self.positions.values() if p.symbol in sym},
                "since_loss": since, "exit_now": sorted(self.exit_now)}
