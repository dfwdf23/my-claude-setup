"""시장분석 에이전트 + 종목선정 에이전트(느린 경로).

시장분석은 2단 구조다.
  · 규칙 기반(무료, 매분): 시장 폭·평균 등락·변동성으로 위험 배수를 계산 — 항상 동작.
  · Claude(유료, 가끔): 수치 요약과 뉴스 제목을 종합해 위험 배수·회피 종목을 판단.
    '상황이 실제로 바뀌었을 때만' 호출한다(상태 서명이 같으면 호출 안 함).
Claude의 권한은 '위험을 줄이는 방향'으로만 열려 있다: 배수는 0~1, 진입 차단, 회피 종목.
"""
from __future__ import annotations

from ..brokers.base import Broker
from ..config import Config
from ..engine import Engine
from ..llm import LLM
from ..models import Regime
from .base import Agent

MARKET_SYSTEM = """당신은 한국 주식시장 자동매매 시스템의 '시장분석' 담당이다.
입력은 JSON 한 개다: 감시 종목의 압축 지표(m), 종목별 요약(s), 있으면 지수(idx)와 뉴스 제목(news).
- m.breadth: 상승 추세 종목 비율(인버스 종목은 방향을 뒤집어 계산), m.above_vwap: VWAP 위 비율
- m.avg_chg: 평균 당일 등락률(%), m.vol_x: 평소 대비 변동성 배수
- s 각 항목: [종목코드, 종류(S=주식,E=ETF,I=인버스ETF), 당일등락%, 추세(1/0), RSI]
이 시스템은 매수(롱) 전용이며 하락장에서는 인버스 ETF를 산다. 당신의 판단은 신규 매수의
크기와 허용 여부에만 쓰인다(기존 보유분의 손절·익절은 별도 규칙이 처리한다).
판단 기준:
- risk_mult: 신규 매수 크기 배수(0~1). 평상시 1, 방향성이 불분명하거나 변동성이 과도하면 낮춘다.
- block_entries: 급변·패닉·대형 이벤트 직전 등 신규 매수를 전면 중단해야 할 때만 true.
- avoid: 지금 사면 안 되는 종목코드 목록(예: 급락장에서 지수 추종 ETF, 급등장에서 인버스).
- bias: risk_on | neutral | risk_off
- reason: 한국어 60자 이내 근거 한 줄.
확신이 없으면 neutral, risk_mult 0.7 근처로 답한다. JSON만 출력한다."""

MARKET_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
        "risk_mult": {"type": "number"},
        "block_entries": {"type": "boolean"},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["bias", "risk_mult", "block_entries", "avoid", "reason"],
    "additionalProperties": False,
}


class MarketAnalyst(Agent):
    name = "market"
    role = "시장분석"
    kind = "llm"

    def __init__(self, cfg: Config, llm: LLM) -> None:
        super().__init__()
        self.cfg = cfg
        self.llm = llm
        self.inverse = frozenset(cfg.inverse)
        self.etfs = frozenset(cfg.etfs)
        self.vol_base = 0.0            # 평소 변동성(느린 지수평균)
        self.metrics: dict = {}
        self.last_sig = None
        self.last_llm_ts = 0.0
        self.llm_calls_day = 0
        self.llm_regime: Regime | None = None
        self._rows: list = []
        self._last_note = ""

    def new_day(self) -> None:
        self.llm_calls_day = 0
        self.llm_regime = None
        self.last_sig = None

    # ---------------------------------------------------------------- 규칙 기반
    def measure(self, engine: Engine) -> dict:
        up = above = n = 0
        chg = atrp = 0.0
        rows = []
        for sym, s in engine.sym.items():
            f = s.f
            if not f.ready or s.day_open <= 0 or f.close <= 0:
                continue
            inv = sym in self.inverse
            d = (s.last / s.day_open - 1.0) * 100.0
            trend = f.ema_fast > f.ema_slow
            n += 1
            up += (not trend) if inv else trend
            above += (s.last <= f.vwap) if inv else (s.last > f.vwap)
            chg += -d if inv else d
            atrp += f.atr / f.close
            rows.append([sym, "I" if inv else ("E" if sym in self.etfs else "S"), round(d, 2),
                         int(trend), round(f.rsi)])
        if n == 0:
            return {}
        atrp /= n
        self.vol_base = atrp if self.vol_base == 0.0 else self.vol_base * 0.98 + atrp * 0.02
        self.metrics = {"n": n, "breadth": round(up / n, 2), "above_vwap": round(above / n, 2),
                        "avg_chg": round(chg / n, 2), "vol_x": round(atrp / self.vol_base, 2) if self.vol_base else 1.0}
        self._rows = rows
        return self.metrics

    def rule_regime(self, m: dict, now: float) -> Regime:
        if not m:
            return Regime(note="지표 준비 중", ts=now)
        mult, bias, note, avoid = 1.0, "neutral", "평상", frozenset()
        if m["vol_x"] >= 2.5:
            mult, bias, note = 0.3, "risk_off", f"변동성 급증 x{m['vol_x']}"
        elif m["vol_x"] >= 1.8:
            mult, note = 0.6, f"변동성 확대 x{m['vol_x']}"
        if m["avg_chg"] <= -2.5:
            bias, note = "risk_off", f"급락장 {m['avg_chg']}% — 지수 추종 종목 매수 회피"
            mult = min(mult, 0.5)
            avoid = frozenset(s for s in self.cfg.universe if s not in self.inverse)
        elif m["breadth"] >= 0.6 and m["avg_chg"] > 0.3 and mult == 1.0:
            bias, note = "risk_on", "상승 우위"
        elif m["breadth"] <= 0.3 and mult == 1.0:
            mult, note = 0.7, "하락 우위"
        return Regime(bias, mult, False, note, "rule", now, avoid)

    # ---------------------------------------------------------------- 갱신
    async def update(self, engine: Engine, broker: Broker, force: bool = False) -> Regime:
        self.calls += 1
        now = engine.now
        m = self.measure(engine)
        rule = self.rule_regime(m, now)
        reg = rule
        if m and self._should_ask(m, now, force):
            got = await self._ask(m, broker, now)
            if got is not None:
                self.llm_regime = got
        lr = self.llm_regime
        if lr is not None and now - lr.ts < self.cfg.llm.market_min_interval_min * 60 * 4:
            # Claude 판단을 쓰되, 규칙이 더 보수적이면 규칙을 따른다
            reg = Regime(lr.bias, min(lr.risk_mult, rule.risk_mult), lr.block_entries,
                         lr.note, "llm", lr.ts, lr.avoid | rule.avoid)
        engine.regime = reg                      # 참조 교체 — 핫패스는 다음 판단부터 새 값을 읽는다
        if reg.note != self._last_note:
            self._last_note = reg.note
            ko = {"risk_on": "위험 선호", "neutral": "중립", "risk_off": "위험 회피"}.get(reg.bias, reg.bias)
            self.note(f"{ko} · 매수 크기 ×{reg.risk_mult:.2f} · {reg.note}", now)
        self.status = "warn" if reg.block_entries or reg.risk_mult < 0.5 else "ok"
        return reg

    def _should_ask(self, m: dict, now: float, force: bool) -> bool:
        c = self.cfg.llm
        if not self.llm.enabled or self.llm_calls_day >= c.market_max_calls:
            return False
        # 상태 서명: 굵게 구간화한 지표. 서명이 그대로면 물어봐도 답이 같으므로 호출하지 않는다.
        sig = (round(m["breadth"] * 4), round(m["avg_chg"]), round(m["vol_x"] * 2))
        if force:
            self.last_sig = sig
            return True
        if sig == self.last_sig or now - self.last_llm_ts < c.market_min_interval_min * 60:
            return False
        self.last_sig = sig
        return True

    async def _ask(self, m: dict, broker: Broker, now: float) -> Regime | None:
        payload = {"m": m, "s": self._rows}
        try:
            idx = await broker.get_market()
            news = await broker.get_headlines(12)
        except Exception:
            idx, news = {}, []
        if idx:
            payload["idx"] = idx
        if news:
            payload["news"] = [t[:50] for t in news]
        self.last_llm_ts = now
        self.llm_calls_day += 1
        c = self.cfg.llm
        d = await self.llm.ask_json("market", c.model_market, c.effort_market, MARKET_SYSTEM,
                                    payload, MARKET_SCHEMA, max_tokens=4096)
        if d is None:
            return None
        try:
            mult = max(0.0, min(1.0, float(d["risk_mult"])))
        except (KeyError, TypeError, ValueError):
            return None
        known = set(self.cfg.universe)
        avoid = frozenset(s for s in d.get("avoid", []) if s in known)
        bias = d.get("bias") if d.get("bias") in ("risk_on", "neutral", "risk_off") else "neutral"
        return Regime(bias, mult, bool(d.get("block_entries")), str(d.get("reason", ""))[:80],
                      "llm", now, avoid)


class Screener(Agent):
    """종목선정 — 후보 중 거래대금이 크고 변동성이 적당한 종목만 감시 대상으로 고른다."""
    name = "screener"
    role = "종목선정"
    kind = "slow"

    def __init__(self, n_daily: int = 60) -> None:
        super().__init__()
        self.n_daily = n_daily
        self.bars: dict[str, list] = {}       # 종목별 일봉(장전 준비 때 일봉 맥락 계산에 재사용)

    async def select(self, broker: Broker, candidates: list[str], max_n: int, now: float) -> list[str]:
        self.calls += 1
        scored = []
        for sym in candidates:
            try:
                bars = await broker.get_daily_bars(sym, self.n_daily)
            except Exception:
                bars = []
            self.bars[sym] = bars
            if len(bars) < 5:
                scored.append((0.0, sym))               # 데이터가 없으면 설정 순서를 존중
                continue
            bars = bars[-20:]
            turnover = sum(b.close * b.volume for b in bars) / len(bars)
            rng = sum((b.high - b.low) / b.close for b in bars if b.close > 0) / len(bars)
            if rng < 0.004 or rng > 0.09:               # 너무 조용하거나 너무 거친 종목 제외
                continue
            scored.append((turnover, sym))
        if not scored:
            return candidates[:max_n]
        order = {s: i for i, s in enumerate(candidates)}
        scored.sort(key=lambda x: (-x[0], order[x[1]]))
        picked = [s for _, s in scored[:max_n]]
        self.note(f"{len(picked)}종목 선정", now)
        return picked
