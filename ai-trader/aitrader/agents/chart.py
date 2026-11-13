"""차트분석 에이전트 — 봉이 마감될 때마다 매수 신호 점수를 계산한다.

LLM을 쓰지 않는다. 지표 계산과 점수화는 수치 연산이라 규칙 엔진이 LLM보다
수십만 배 빠르고(마이크로초 vs 초) 결과도 결정적이다. LLM은 이 점수의 "기준값"을
장후 복기에서 조정하는 데만 쓰인다.

점수 구성(합 1.0):
  분봉  — 추세(단기>장기 EMA) · VWAP 위 · N봉 고가 돌파 · RSI 위치 · 봉 거래량
  맥락  — 상대 거래량(RVOL: 평소 이 시각 대비 오늘 누적 거래량) · 일봉 추세 · 전일 고가 돌파
"""
from __future__ import annotations

from ..models import Features, Signal
from ..params import StrategyParams
from .base import Agent

W_TREND, W_VWAP, W_BREAK, W_RSI, W_VOL = 0.24, 0.12, 0.20, 0.08, 0.12
W_RVOL, W_DAILY, W_PREV = 0.12, 0.06, 0.06


def _clip(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


class ChartAnalyst(Agent):
    name = "chart"
    role = "차트분석"

    def evaluate(self, symbol: str, f: Features, p: StrategyParams, rvol: float = -1.0,
                 dtrend: float = 0.5, prev_brk: float = 0.5) -> Signal | None:
        """rvol < 0 은 '일봉 자료 없음' — 그 요소는 중립(0.5)으로 두고 거르지 않는다."""
        self.calls += 1
        atr = f.atr
        if atr <= 0.0:
            return None
        # 필수 조건: 상승 추세 + 최소 모멘텀 + (알 수 있다면) 최소 상대 거래량
        if f.ema_fast <= f.ema_slow or f.close <= f.ema_slow or f.rsi < p.rsi_min:
            return None
        if 0.0 <= rvol < p.rvol_min:
            return None
        trend = _clip((f.ema_fast - f.ema_slow) / atr)
        vwap = 1.0 if f.close > f.vwap else 0.0
        if f.close > f.hh:
            brk = 1.0
        else:
            brk = 0.5 * _clip(1.0 - (f.hh - f.close) / atr)
        span = p.rsi_overbought - p.rsi_min
        rsi = _clip((f.rsi - p.rsi_min) / (span * 0.5)) if span > 0 else 0.0
        vol = _clip((f.vol_ratio - 0.5) / (p.vol_ratio_min * 1.5 - 0.5))
        rv = 0.5 if rvol < 0.0 else _clip((rvol - 0.5) / 1.5)
        score = (W_TREND * trend + W_VWAP * vwap + W_BREAK * brk + W_RSI * rsi + W_VOL * vol
                 + W_RVOL * rv + W_DAILY * dtrend + W_PREV * prev_brk)
        if score < p.entry_score_min:
            return None
        return Signal(symbol, score, {"trend": round(trend, 3), "vwap": vwap, "brk": round(brk, 3),
                                      "rsi_s": round(rsi, 3), "vol_s": round(vol, 3), "rvol_s": round(rv, 3)})
