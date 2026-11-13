"""복기(학습) 에이전트 — 손실 원인을 분석하고, 검증을 통과한 개선안만 다음 날부터 적용한다.

절차(장 마감 후 1회):
  1. 손실 거래마다 원인 태그를 붙인다(규칙 기반, 무료).
  2. 원인별 손실액을 집계해 '가장 비싼 실수'를 찾는다.
  3. 개선 후보를 만든다 — 규칙 기반 후보 + (가능하면) Claude가 통계를 보고 낸 후보.
  4. 후보마다 최근 며칠의 실제 기록 봉으로 재생(백테스트)한다. 기간을 둘로 나눠
     양쪽 모두에서 나빠지지 않고 전체로 개선될 때만 채택한다(과최적화 방지).
  5. 채택된 값은 버전을 올려 저장한다. 새 버전의 실전 성과가 이전보다 나쁘면 자동 롤백한다.
학습이 바꿀 수 있는 것은 params.BOUNDS 안의 전략 파라미터뿐이다. 위험 한도는 못 건드린다.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict

from ..backtest import Backtester
from ..config import Config
from ..llm import LLM
from ..metrics import score, summarize
from ..models import Bar, Trade
from ..params import BOUNDS, ParamStore, StrategyParams, apply_changes, diff
from .base import Agent

# 원인 → 파라미터 조정(증감)
NUDGES: dict[str, dict[str, float]] = {
    "과열 추격": {"rsi_overbought": -2.0, "max_ext_atr": -0.2},
    "빠른 손절(손절폭 부족)": {"stop_atr_mult": 0.25},
    "수익 반납": {"trail_atr_mult": -0.25, "trail_start_r": -0.2},
    "약한 추세 진입": {"entry_score_min": 0.03},
    "거래량 미확인": {"vol_ratio_min": 0.15},
    "돌파 없는 진입": {"entry_score_min": 0.02},
    "비용 손실": {"take_profit_r": 0.3},
    "시간 청산": {"time_stop_bars": -10.0},
    "추세 이탈": {"entry_score_min": 0.02, "rsi_min": 2.0},
    "시장 역행": {},
    "장 초반 진입": {},
    "장 후반 진입": {},
}

REVIEW_SYSTEM = """당신은 한국 주식 자동매매 시스템의 '복기' 담당이다. 오늘과 최근 거래 통계(JSON)를 보고
손실의 원인을 진단하고, 전략 파라미터 조정안을 제안한다.
입력 필드: params(현재값), bounds(파라미터별 [최소,최대,1회 최대 변경폭]), summary(전체 성과),
by_exit(청산사유별 [건수,손익,평균R]), by_cause(규칙 기반 손실원인별 [건수,손익]),
by_hour(진입 시각별 [건수,손익]), win_avg/loss_avg(이긴/진 거래의 진입 시점 평균 지표),
worst(가장 나쁜 거래들: [종목,R,청산사유,RSI,이격(ATR배수),추세강도,거래량비,진입시각,보유봉수,MAE(R),MFE(R)]),
blocks(과매수 방지·리스크 차단 사유별 횟수).
파라미터 뜻: entry_score_min(매수 최소 점수), rsi_min/rsi_overbought(매수 허용 RSI 범위),
max_ext_atr(단기 평균 대비 이격 상한), vol_ratio_min(거래량 확인 기준), stop_atr_mult(손절폭),
take_profit_r(익절 배수), trail_start_r/trail_atr_mult(추적손절 시작·간격), time_stop_bars(시간 청산),
cooldown_bars(손절 후 재진입 금지).
규칙:
- 제안은 최대 3개. 각 제안은 서로 다른 가설이어야 하고, 파라미터는 bounds 범위와 변경폭 안에서만.
- 표본이 적으면(거래 10건 미만) 무리하게 바꾸지 말고 제안을 비워도 된다.
- 제안은 시스템이 과거 기록으로 검증한 뒤 통과한 것만 적용하므로, 근거가 통계에 있는 것만 낸다.
- lessons는 한국어로 최대 3줄, 각 70자 이내. 숫자 근거를 포함한다.
JSON만 출력한다."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": {"type": "string"}},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"param": {"type": "string"}, "value": {"type": "number"}},
                            "required": ["param", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "why", "changes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lessons", "proposals"],
    "additionalProperties": False,
}


def tag_causes(t: Trade, p: StrategyParams) -> list[str]:
    """손실 거래 1건의 원인 태그(여러 개 가능)."""
    if t.pnl >= 0:
        return []
    c = t.ctx
    out = []
    if (c.get("rsi", 0) >= p.rsi_overbought - 6 or c.get("ext_atr", 0) >= 0.75 * p.max_ext_atr
            or c.get("day_gain", 0) >= 0.7 * p.max_day_gain_pct):
        out.append("과열 추격")
    if t.exit_reason == "stop_loss" and t.bars_held <= 3:
        out.append("빠른 손절(손절폭 부족)")
    if t.mfe_r >= 1.0:
        out.append("수익 반납")
    if c.get("trend", 1.0) < 0.35:
        out.append("약한 추세 진입")
    if c.get("vol_ratio", 9.9) < p.vol_ratio_min:
        out.append("거래량 미확인")
    if c.get("breakout", 1.0) < 1.0:
        out.append("돌파 없는 진입")
    if (t.exit_price - t.entry_price) > 0:
        out.append("비용 손실")
    if t.exit_reason == "time_stop":
        out.append("시간 청산")
    if t.exit_reason == "trend_exit":
        out.append("추세 이탈")
    if c.get("regime") == "risk_off":
        out.append("시장 역행")
    tod = c.get("tod", 0)
    if 0 < tod < 9 * 3600 + 1800:
        out.append("장 초반 진입")
    elif tod >= 14 * 3600:
        out.append("장 후반 진입")
    return out or ["기타"]


def _avg(trades: list[Trade], key: str) -> float:
    v = [t.ctx.get(key) for t in trades if isinstance(t.ctx.get(key), (int, float))]
    return round(sum(v) / len(v), 2) if v else 0.0


class Learner(Agent):
    name = "learner"
    role = "복기·학습"
    kind = "llm"

    def __init__(self, cfg: Config, store: ParamStore, llm: LLM, journal=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.journal = journal
        self.reports: list[dict] = []
        self.day_pnls: list[float] = []
        self.days_since_change = 10 ** 6

    # ---------------------------------------------------------------- 통계
    def analyze(self, trades: list[Trade], p: StrategyParams) -> dict:
        by_exit: dict = defaultdict(lambda: [0, 0.0, 0.0])
        by_cause: dict = defaultdict(lambda: [0, 0.0])
        by_hour: dict = defaultdict(lambda: [0, 0.0])
        for t in trades:
            e = by_exit[t.exit_reason]
            e[0] += 1
            e[1] += t.pnl
            e[2] += t.r_multiple
            h = by_hour[str(int(t.ctx.get("tod", 0)) // 3600)]
            h[0] += 1
            h[1] += t.pnl
            t.causes = tag_causes(t, p)
            for c in t.causes:
                bc = by_cause[c]
                bc[0] += 1
                bc[1] += t.pnl
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        keys = ("score", "rsi", "ext_atr", "trend", "vol_ratio")
        worst = sorted(losses, key=lambda t: t.pnl)[:8]
        return {
            "summary": summarize(trades),
            "by_exit": {k: [v[0], round(v[1]), round(v[2] / v[0], 2)] for k, v in by_exit.items()},
            "by_cause": {k: [v[0], round(v[1])] for k, v in sorted(by_cause.items(), key=lambda kv: kv[1][1])},
            "by_hour": {k: [v[0], round(v[1])] for k, v in sorted(by_hour.items())},
            "win_avg": {k: _avg(wins, k) for k in keys},
            "loss_avg": {k: _avg(losses, k) for k in keys},
            "worst": [[t.symbol, round(t.r_multiple, 2), t.exit_reason, t.ctx.get("rsi", 0),
                       t.ctx.get("ext_atr", 0), t.ctx.get("trend", 0), t.ctx.get("vol_ratio", 0),
                       f"{int(t.ctx.get('tod', 0)) // 3600:02d}:{int(t.ctx.get('tod', 0)) % 3600 // 60:02d}",
                       t.bars_held, round(t.mae_r, 2), round(t.mfe_r, 2)] for t in worst],
        }

    def rule_candidates(self, stats: dict, p: StrategyParams) -> list[tuple[str, dict, str]]:
        """손실액이 큰 원인부터 조정 후보를 만든다. (이름, 변경안, 근거)"""
        out = []
        ranked = [(c, v) for c, v in stats["by_cause"].items() if NUDGES.get(c)]
        for cause, (n, pnl) in ranked[:3]:
            ch = {k: getattr(p, k) + dv for k, dv in NUDGES[cause].items()}
            out.append((f"규칙:{cause}", ch, f"{cause} {n}건 {pnl:+,.0f}원"))
        if len(ranked) >= 2:
            ch = {}
            for cause, _ in ranked[:2]:
                ch.update({k: getattr(p, k) + dv for k, dv in NUDGES[cause].items()})
            out.append((f"규칙:{ranked[0][0]}+{ranked[1][0]}", ch, "상위 두 원인 동시 조정"))
        s = stats["summary"]
        if s["n"] and s["win_rate"] >= 0.55 and s["pf"] >= 1.5:
            # 잘 되고 있으면 기회를 조금 넓혀 보는 후보도 검증대에 올린다
            out.append(("규칙:진입 완화", {"entry_score_min": p.entry_score_min - 0.02}, "승률·손익비 양호"))
        return out

    # ---------------------------------------------------------------- 검증
    def validate(self, base: StrategyParams, cands: list[tuple[str, StrategyParams, str]],
                 bars: list[Bar]) -> tuple[dict | None, list[dict]]:
        """기록된 봉을 재생해 후보를 비교한다. (채택안 또는 None, 비교표)"""
        days = sorted({int((b.ts + 32400) // 86400) for b in bars})
        if len(days) < 2 or not cands:
            return None, []
        mid = days[len(days) // 2]
        halves = [[b for b in bars if int((b.ts + 32400) // 86400) < mid],
                  [b for b in bars if int((b.ts + 32400) // 86400) >= mid]]
        cap = self.cfg.capital

        def run(p: StrategyParams) -> tuple[list[float], int, dict]:
            scores, n, pnl, dd = [], 0, 0.0, 0.0
            for h in halves:
                m = Backtester(self.cfg, p, cap).run_bars(h).result()
                scores.append(score(m, cap))
                n += m["n"]
                pnl += m["pnl"]
                dd = max(dd, m["max_dd"])
            return scores, n, {"n": n, "pnl": round(pnl), "max_dd": round(dd)}

        b_scores, b_n, b_m = run(base)
        b_total = sum(b_scores)
        table = [{"name": "현재값", "score": round(b_total, 5), **b_m}]
        need = b_total + max(self.cfg.learn.min_improve * abs(b_total), 0.0005)
        best = None
        for name, p, why in cands:
            sc, n, m = run(p)
            total = sum(sc)
            ok = (total > need and all(s >= b - 0.002 for s, b in zip(sc, b_scores))
                  and n >= 0.4 * b_n)
            table.append({"name": name, "score": round(total, 5), **m, "ok": ok})
            if ok and (best is None or total > best["score"]):
                best = {"name": name, "params": p, "why": why, "score": total, "base": b_total, **m}
        return best, table

    # ---------------------------------------------------------------- 장후 복기
    async def review(self, day: str, trades_day: list[Trade], recent: list[Trade],
                     bars: list[Bar], blocks: dict, now: float) -> dict:
        self.calls += 1
        cfg = self.cfg.learn
        p = self.store.current
        stats = self.analyze(recent, p)
        today = summarize(trades_day)
        self.day_pnls.append(today["pnl"])
        report = {"day": day, "today": today, "recent": stats["summary"], "version": p.version,
                  "causes": [[k, v[0], v[1]] for k, v in list(stats["by_cause"].items())[:5]],
                  "lessons": [], "adopted": None, "tested": [], "source": "rule"}

        # 0) 직전 변경이 실전에서 더 나빴으면 되돌린다
        rb = self._maybe_rollback(recent)
        if rb:
            report["lessons"].append(rb)
            report["adopted"] = {"name": "롤백", "changes": {}, "why": rb}
            p = self.store.current

        # 1) 후보 생성: 규칙 + Claude
        cands: list[tuple[str, StrategyParams, str]] = []
        enough = len(recent) >= cfg.min_trades
        self.days_since_change += 1
        settled = self.days_since_change >= cfg.min_days_between_changes
        if rb:
            self.days_since_change = 0
        if enough and not rb and not settled:
            report["lessons"].append(
                f"v{p.version} 적용 {self.days_since_change}일째 — 효과 측정 중이라 오늘은 변경하지 않음")
        if enough and not rb and settled:
            for name, ch, why in self.rule_candidates(stats, p):
                cands.append((name, apply_changes(p, ch), why))
            llm_out = await self._ask_llm(stats, p, blocks)
            if llm_out:
                report["source"] = "llm"
                report["lessons"] += [str(x)[:90] for x in llm_out.get("lessons", [])[:3]]
                for pr in llm_out.get("proposals", [])[:3]:
                    ch = {c["param"]: c["value"] for c in pr.get("changes", []) if c.get("param") in BOUNDS}
                    if ch:
                        cands.append((f"Claude:{str(pr.get('name', ''))[:24]}", apply_changes(p, ch),
                                      str(pr.get("why", ""))[:90]))
        report["lessons"] = self._rule_lessons(stats, today, enough) + report["lessons"]

        # 2) 검증 → 채택
        cands = [c for c in cands if diff(p, c[1])]
        if cands and bars:
            best, table = await asyncio.to_thread(self.validate, p, cands, bars)
            report["tested"] = table
            if best is not None:
                changes = {k: list(v) for k, v in diff(p, best["params"]).items()}
                reason = f"{best['name']} — {best['why']} (검증 점수 {best['base']:+.4f} → {best['score']:+.4f})"
                self.store.adopt(best["params"], reason, {"score": best["score"], "base": best["base"]})
                self.days_since_change = 0
                report["adopted"] = {"name": best["name"], "changes": changes, "why": best["why"]}
                report["lessons"].append(f"채택: {best['name']} · " + ", ".join(
                    f"{k} {a}→{b}" for k, (a, b) in changes.items()))
            elif table:
                report["lessons"].append(f"후보 {len(table) - 1}개 검증 — 현재값보다 확실히 나은 안이 없어 유지")

        # 3) 성과 기반 크기 조절(연속 손실일이면 줄이고, 회복하면 되돌림)
        thr = self._throttle()
        if thr:
            report["lessons"].append(thr)

        report["version_after"] = self.store.current.version
        self.reports.append(report)
        del self.reports[:-30]
        for line in report["lessons"]:
            if self.journal is not None:
                self.journal.lesson(now, day, report["source"], line,
                                    report["adopted"]["changes"] if report["adopted"] else {})
        self.note(f"{day} 복기: {today['n']}건 {today['pnl']:+,.0f}원 · v{report['version_after']}", now)
        return report

    def _maybe_rollback(self, recent: list[Trade]) -> str:
        cur = self.store.current
        if len(self.store.history) < 2:
            return ""
        prev_v = self.store.history[-2]["version"]
        cur_t = [t for t in recent if t.params_version == cur.version]
        prev_t = [t for t in recent if t.params_version == prev_v]
        cur_days = {int((t.exit_ts + 32400) // 86400) for t in cur_t}
        if (len(cur_t) < self.cfg.learn.rollback_min_trades or len(prev_t) < 5
                or len(cur_days) < self.cfg.learn.rollback_min_days):
            return ""
        ce = sum(t.r_multiple for t in cur_t) / len(cur_t)
        pe = sum(t.r_multiple for t in prev_t) / len(prev_t)
        if ce < 0 and ce < pe - 0.1:
            self.store.rollback(f"v{cur.version} 기대값 {ce:+.2f}R < v{prev_v} {pe:+.2f}R")
            return f"롤백: v{cur.version} 실전 기대값 {ce:+.2f}R 이 이전 {pe:+.2f}R 보다 나빠 되돌림"
        return ""

    def _throttle(self) -> str:
        p = self.store.current
        last3 = self.day_pnls[-3:]
        new = p.size_mult
        if len(last3) == 3 and all(x < 0 for x in last3):
            new = max(BOUNDS["size_mult"][0], p.size_mult - 0.25)
        elif self.day_pnls and self.day_pnls[-1] > 0 and p.size_mult < 1.0:
            new = min(1.0, p.size_mult + 0.25)
        if new == p.size_mult:
            return ""
        old = p.size_mult
        self.store.tweak("size_mult", new)
        return (f"3일 연속 손실 — 거래 크기 {old:.2f}→{new:.2f}배로 축소" if new < old
                else f"수익 회복 — 거래 크기 {old:.2f}→{new:.2f}배로 복원")

    def _rule_lessons(self, stats: dict, today: dict, enough: bool) -> list[str]:
        out = []
        if today["n"] == 0:
            out.append("오늘 거래 없음 — 조건에 맞는 신호가 없었거나 리스크 한도로 대기")
        if not enough:
            out.append(f"표본 부족(최근 {stats['summary']['n']}건) — 파라미터는 건드리지 않음")
            return out
        for cause, (n, pnl) in list(stats["by_cause"].items())[:2]:
            out.append(f"최대 손실 원인: {cause} {n}건 {pnl:+,.0f}원")
        return out

    async def _ask_llm(self, stats: dict, p: StrategyParams, blocks: dict) -> dict | None:
        if not self.llm.enabled:
            return None
        pd = asdict(p)
        pd.pop("version", None)
        payload = {"params": pd, "bounds": {k: list(v) for k, v in BOUNDS.items() if k != "size_mult"},
                   "blocks": blocks, **stats}
        c = self.cfg.llm
        return await self.llm.ask_json("review", c.model_review, c.effort_review, REVIEW_SYSTEM,
                                       payload, REVIEW_SCHEMA, max_tokens=8192)
