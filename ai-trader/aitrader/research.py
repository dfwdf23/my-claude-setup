"""실제 과거 시세로 전략을 검증한다(합성 시세가 아니라).

입력: 야후 파이낸스 차트 API 형식의 JSON 파일(<종목>_<간격>.json, 일봉은 <종목>_1d_1y.json).
  예) curl "https://query1.finance.yahoo.com/v8/finance/chart/NVDA?interval=5m&range=60d" > NVDA_5m_60d.json
야후 차트 API는 비공식이며 약관상 개인적 조사 용도로만 써야 한다. 실매매 시세로는 쓰지 않는다.

방식: 실매매와 같은 엔진에 분봉을 재생한다(봉 재생의 보수적 가정은 backtest.py 참고).
날마다 '그 전날까지의' 일봉으로 일봉 맥락을 주므로 미래 정보는 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

from .agents.learner import Learner
from .backtest import Backtester
from .config import Config
from .journal import Journal
from .llm import LLM
from .metrics import summarize
from .models import Bar
from .params import ParamStore


def load_yahoo(path: Path, symbol: str) -> list[Bar]:
    d = json.loads(path.read_text())
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c) or c <= 0:
            continue
        out.append(Bar(symbol, float(ts), float(o), float(h), float(l), float(c), int(v or 0)))
    return out


def load_dir(folder: str, interval: str, symbols: list[str]) -> tuple[dict, dict, int]:
    """(일자별 분봉, 종목별 일봉, 분봉 길이(초))"""
    base = Path(folder)
    intraday: dict[str, list[Bar]] = {}
    daily: dict[str, list[Bar]] = {}
    for sym in symbols:
        p = base / f"{sym}_{interval}.json"
        if p.exists():
            intraday[sym] = load_yahoo(p, sym)
        pd = base / f"{sym}_1d_1y.json"
        if pd.exists():
            daily[sym] = load_yahoo(pd, sym)
    if not intraday:
        raise SystemExit(f"{folder} 에 *_{interval}.json 파일이 없습니다")
    any_bars = next(iter(intraday.values()))
    step = min(b.ts - a.ts for a, b in zip(any_bars, any_bars[1:]) if b.ts > a.ts)
    return intraday, daily, int(step)


def run_research(cfg: Config, folder: str, interval: str = "5m_60d", commission: float | None = None,
                 learn: bool = False, quiet: bool = False, no_halt: bool = False,
                 start: str | None = None, end: str | None = None) -> dict:
    """start/end: 'YYYY-MM-DD' (현지 날짜, end 는 포함하지 않음). no_halt: 손실 한도 정지를 끄고 전 기간을 본다."""
    from datetime import date, timedelta
    m = cfg.mkt
    intraday, daily, step = load_dir(folder, interval, cfg.universe)
    cfg.strategy.bar_sec = step
    if step >= 3600:
        # 긴 봉을 재생할 때는 '다음 봉 시가'에 판단·체결한 것으로 본다(실매매는 매수 시작 시각의 가격).
        cfg.session.entry_start = cfg.session.open
    if commission is not None:
        cfg.fees.commission = commission
    if no_halt:
        cfg.risk.max_drawdown = 1.0
        cfg.risk.max_daily_loss = 1.0
    d_lo = (date.fromisoformat(start) - date(1970, 1, 1)).days if start else None
    d_hi = (date.fromisoformat(end) - date(1970, 1, 1)).days if end else None
    by_day: dict[int, list[Bar]] = defaultdict(list)
    for bars in intraday.values():
        for b in bars:
            by_day[m.local_day(b.ts)].append(b)
    days = [d for d in sorted(by_day) if (d_lo is None or d >= d_lo) and (d_hi is None or d < d_hi)]
    store = ParamStore(None, cfg.params)
    bt = Backtester(cfg, store.current)
    e = bt.engine
    learner = Learner(cfg, store, LLM(_Off()), Journal(None)) if learn else None
    day_rows = []
    recorded: list[Bar] = []
    for d in days:
        day0 = d * 86400.0 - m.utc_offset(d * 86400.0 + 43200.0)       # 그날 현지 0시의 epoch
        for sym, dbars in daily.items():
            e.set_daily(sym, [b for b in dbars if b.ts < day0][-60:])
        bars = sorted(by_day[d], key=lambda b: (b.ts, b.symbol))
        eq0, n0 = e.equity, len(e.trades)
        bt.run_bars(bars)
        e.flush_bars()
        e.mark()
        td = e.trades[n0:]
        day_rows.append({"day": d, "pnl": e.equity - eq0, "n": len(td), "v": e.p.version, "eq": e.equity})
        if learner is not None:
            recorded = (recorded + bars)[-len(cfg.universe) * 400 * cfg.learn.lookback_days:]
            keep = sorted({m.local_day(b.ts) for b in recorded})[-cfg.learn.lookback_days:]
            rec = [b for b in recorded if m.local_day(b.ts) >= keep[0]]
            asyncio.run(learner.review(str(d), td, e.trades[-300:], rec, {}, e.now))
            e.set_params(store.current)
    r = bt.result()
    peak, mdd = bt.start, 0.0
    years: dict[int, float] = defaultdict(float)
    for x in day_rows:
        peak = max(peak, x["eq"])
        mdd = max(mdd, 1.0 - x["eq"] / peak)
        years[(date(1970, 1, 1) + timedelta(days=x["day"])).year] += x["pnl"] / bt.start
    total_ret = e.equity / bt.start - 1.0
    hold = {}                                           # 같은 기간 단순 보유 수익률(비교 기준)
    for sym, bars in intraday.items():
        bs = [b for b in bars if m.local_day(b.ts) in set(days)]
        if bs:
            hold[sym] = bs[-1].close / bs[0].open - 1.0
    by_sym = defaultdict(list)
    by_exit = defaultdict(list)
    for t in e.trades:
        by_sym[t.symbol].append(t)
        by_exit[t.exit_reason].append(t)
    out = {"result": r, "days": len(days), "bar_sec": step, "fees": e.fees_total, "halted": e.risk.halted,
           "ret": total_ret, "mdd": mdd, "years": dict(years), "hold": hold, "open": len(e.positions),
           "halt_reason": e.risk.halt_reason, "win_days": sum(1 for x in day_rows if x["pnl"] > 0),
           "by_symbol": {s: summarize(ts) for s, ts in sorted(by_sym.items())},
           "by_exit": {k: summarize(ts) for k, ts in sorted(by_exit.items())},
           "version": e.p.version, "history": [h["reason"] for h in store.history[1:]],
           "blocks": {**e.guard.blocks, **e.risk.blocks}}
    if not quiet:
        print(f"[실제 시세 {len(days)}거래일 · {len(intraday)}종목 · {step // 60}분봉 · 수수료 {cfg.fees.commission:.2%}"
              f"{' · 학습 켬' if learn else ''}]")
        print(f"자산 수익률 {total_ret:+.2%} · 자산 최대낙폭 {mdd:.1%} · 청산 거래 {r['n']}건 · 승률 {r['win_rate']:.0%} · "
              f"손익비 {r['pf']} · 기대값 {r['exp_r']:+.2f}R · 비용 {m.money(e.fees_total, signed=False)} · "
              f"보유 중 {len(e.positions)}종목" + (f" · 정지: {e.risk.halt_reason}" if e.risk.halted else ""))
        print("  연도별: " + " · ".join(f"{y} {v:+.1%}" for y, v in sorted(years.items())))
        if hold:
            avg = sum(hold.values()) / len(hold)
            print(f"  비교(같은 기간 단순 보유): 평균 {avg:+.0%} — " + " · ".join(f"{s} {v:+.0%}" for s, v in hold.items()))
        for k, s in out["by_exit"].items():
            print(f"  청산 {k:14} {s['n']:4}건 {m.money(s['pnl']):>12} 평균 {s['exp_r']:+.2f}R")
        print("  종목별: " + " · ".join(f"{s} {v['n']}건 {m.money(v['pnl'])}" for s, v in out["by_symbol"].items()))
        if learn:
            print(f"  전략 v{e.p.version} · 변경 {len(out['history'])}회")
    return out


class _Off:
    enabled = False
