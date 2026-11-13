"""과매수 방지 에이전트 + 리스크 관리 에이전트. 둘 다 '거부권'만 가진다."""
from __future__ import annotations

from collections import Counter, deque

from ..config import RiskCfg, SessionCfg
from ..models import BUY, Features, OrderIntent, Regime, Trade
from ..params import StrategyParams
from .base import Agent


class OverboughtGuard(Agent):
    """과매수 방지 — 이미 과열된 종목을 뒤늦게 추격 매수하지 않게 막는다."""
    name = "overbought"
    role = "과매수 방지"

    def __init__(self) -> None:
        super().__init__()
        self.blocks: Counter = Counter()

    def check(self, f: Features, day_gain_pct: float, bars_since_stop: int,
              p: StrategyParams) -> str:
        """통과면 빈 문자열, 차단이면 사유."""
        self.calls += 1
        why = ""
        if f.rsi >= p.rsi_overbought:
            why = "RSI 과매수"
        elif f.atr > 0 and (f.close - f.ema_fast) / f.atr > p.max_ext_atr:
            why = "이격 과다"
        elif f.bb_upper > 0 and f.close > f.bb_upper and f.rsi >= p.rsi_overbought - 5:
            why = "밴드 상단 이탈"
        elif day_gain_pct >= p.max_day_gain_pct:
            why = "당일 급등 추격"
        elif bars_since_stop < p.cooldown_bars:
            why = "손절 직후 재진입"
        if why:
            self.blocks[why] += 1
        return why


class RiskManager(Agent):
    """리스크 관리 — 계좌 단위 한도. 학습 에이전트가 수정할 수 없는 고정 규칙."""
    name = "risk"
    role = "리스크 관리"

    def __init__(self, cfg: RiskCfg, session: SessionCfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.entry_start = session.tod("entry_start")
        self.entry_end = session.tod("entry_end")
        self.halted = False          # 완전 정지(수동 해제 전까지 유지)
        self.halt_reason = ""
        self.day_stop = False        # 당일 손실 한도 도달 → 오늘은 신규 매수 금지
        self.flatten_all = False     # 매도 에이전트에게 전량 청산 지시
        self.feed_stale = False
        self.start_equity = 0.0
        self.peak_equity = 0.0
        self.drawdown = 0.0
        self.trades_today = 0
        self.consec_losses = 0
        self.cooldown_until = 0.0
        self.order_ts: deque = deque()
        self.reject_ts: deque = deque()
        self.blocks: Counter = Counter()

    def new_day(self, equity: float) -> None:
        self.start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        self.day_stop = False
        if not self.halted:
            self.flatten_all = False
        self.trades_today = 0
        self.consec_losses = 0
        self.cooldown_until = 0.0

    def halt(self, reason: str, flatten: bool = True) -> None:
        self.halted = True
        self.halt_reason = reason
        self.flatten_all = self.flatten_all or flatten
        self.status = "halt"

    def resume(self, equity: float = 0.0) -> None:
        """수동 재개. 기준점을 현재 자산으로 다시 잡는다 — 그러지 않으면 낙폭·일일 손실이
        여전히 한도를 넘은 상태라 재개하자마자 다시 정지된다."""
        self.halted = False
        self.halt_reason = ""
        self.flatten_all = False
        self.day_stop = False
        self.status = "ok"
        self.reject_ts.clear()
        if equity > 0:
            self.peak_equity = equity
            self.start_equity = equity
            self.drawdown = 0.0

    def export(self) -> dict:
        return {"halted": self.halted, "halt_reason": self.halt_reason, "day_stop": self.day_stop,
                "flatten_all": self.flatten_all, "peak_equity": self.peak_equity,
                "start_equity": self.start_equity, "trades_today": self.trades_today,
                "consec_losses": self.consec_losses, "cooldown_until": self.cooldown_until}

    def restore(self, d: dict, same_day: bool) -> None:
        """재시작 후 복원. 정지 상태와 고점은 항상, 당일 한도 상태는 같은 거래일일 때만 되살린다."""
        self.peak_equity = float(d.get("peak_equity", self.peak_equity))
        if d.get("halted"):
            self.halt(d.get("halt_reason") or "이전 실행에서 정지됨", flatten=bool(d.get("flatten_all")))
        if same_day:
            self.start_equity = float(d.get("start_equity", self.start_equity))
            self.trades_today = int(d.get("trades_today", 0))
            self.consec_losses = int(d.get("consec_losses", 0))
            self.cooldown_until = float(d.get("cooldown_until", 0.0))
            if d.get("day_stop"):
                self.day_stop = True
                self.flatten_all = True
                self.status = "warn"

    def check_limits(self, equity: float) -> str:
        """주기적으로 호출. 한도 위반 시 사유를 돌려주고 내부 상태를 정지로 바꾼다."""
        if equity > self.peak_equity:
            self.peak_equity = equity
        self.drawdown = 1.0 - equity / self.peak_equity if self.peak_equity > 0 else 0.0
        if not self.halted and self.drawdown >= self.cfg.max_drawdown:
            self.halt(f"최대 낙폭 {self.drawdown:.1%} 도달")
            return self.halt_reason
        if (not self.day_stop and self.start_equity > 0
                and equity <= self.start_equity * (1.0 - self.cfg.max_daily_loss)):
            self.day_stop = True
            self.flatten_all = True
            self.status = "warn"
            return f"일일 손실 한도 {self.cfg.max_daily_loss:.1%} 도달 — 오늘 매매 중단"
        return ""

    def note_order(self, now: float) -> None:
        self.order_ts.append(now)

    def note_reject(self, now: float) -> bool:
        """거부가 몰리면(주문 폭주·계좌 불일치 의심) 정지. 정지했으면 True."""
        q = self.reject_ts
        q.append(now)
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self.cfg.max_rejects_per_min and not self.halted:
            self.halt("주문 거부 반복 — 계좌·한도 점검 필요", flatten=False)
            return True
        return False

    def on_trade(self, t: Trade, now: float) -> None:
        self.trades_today += 1
        if t.pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= self.cfg.max_consec_losses:
                self.cooldown_until = now + self.cfg.cooldown_min * 60.0
                self.consec_losses = 0
                self.note(f"연속 손실 → {self.cfg.cooldown_min}분 냉각", now)
        else:
            self.consec_losses = 0

    def pre_trade(self, it: OrderIntent, last: float, now: float, tod: float,
                  n_positions: int, exposure: float, equity: float, regime: Regime) -> str:
        """주문 직전 검사. 통과면 빈 문자열."""
        self.calls += 1
        c = self.cfg
        why = ""
        notional = it.qty * (it.limit_price or last)
        if it.qty <= 0:
            why = "수량 0"
        elif it.limit_price and last > 0 and abs(it.limit_price / last - 1.0) > c.price_band:
            why = "주문가 이상"
        elif it.side == BUY:          # 매도(위험 축소)는 아래 한도로 막지 않는다
            q = self.order_ts
            while q and now - q[0] > 60.0:
                q.popleft()
            if notional > c.max_order_notional:
                why = "주문금액 상한"
            elif self.halted:
                why = "정지 상태"
            elif self.day_stop:
                why = "일일 손실 한도"
            elif self.feed_stale:
                why = "시세 지연"
            elif regime.block_entries:
                why = "시장 위험회피"
            elif it.symbol in regime.avoid:
                why = "시장분석 회피 종목"
            elif tod < self.entry_start or tod >= self.entry_end:
                why = "매수 시간 외"
            elif now < self.cooldown_until:
                why = "연속 손실 냉각"
            elif n_positions >= c.max_positions:
                why = "보유 종목 수 상한"
            elif self.trades_today + n_positions >= c.max_trades_per_day:
                why = "일일 거래 횟수 상한"
            elif len(q) >= c.max_orders_per_min:
                why = "분당 주문 상한"
            elif exposure + notional > equity * c.max_exposure:
                why = "총 비중 상한"
        if why:
            self.blocks[why] += 1
        return why
