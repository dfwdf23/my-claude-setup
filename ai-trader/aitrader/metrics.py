"""성과 지표."""
from __future__ import annotations


def summarize(trades: list, start_equity: float = 0.0) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "win_rate": 0.0, "pf": 0.0, "exp_r": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "max_dd": 0.0, "ret": 0.0}
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    gw, gl = sum(wins), -sum(losses)
    pnl = gw - gl
    cum = peak = dd = 0.0
    for t in trades:
        cum += t.pnl
        if cum > peak:
            peak = cum
        if peak - cum > dd:
            dd = peak - cum
    return {
        "n": n,
        "pnl": round(pnl, 2),
        "win_rate": round(len(wins) / n, 3),
        "pf": round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0),   # 손익비(총이익/총손실)
        "exp_r": round(sum(t.r_multiple for t in trades) / n, 3),            # 거래당 기대값(R)
        "avg_win": round(gw / len(wins), 2) if wins else 0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0,
        "max_dd": round(dd, 2),
        "ret": round(pnl / start_equity, 5) if start_equity else 0.0,
    }


def score(m: dict, start_equity: float) -> float:
    """파라미터 후보 비교용 단일 점수: 수익률에서 낙폭을 절반 가중으로 뺀 값."""
    if not start_equity:
        return 0.0
    return (m["pnl"] - 0.5 * m["max_dd"]) / start_equity
