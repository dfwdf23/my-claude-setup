"""실행 진입점.

  python -m aitrader run                      # 시뮬레이션(합성 시세, 미국 시장) + 대시보드
  python -m aitrader run --mode kis-mock      # 한국투자증권 모의투자(해외주식)
  python -m aitrader run --mode kis-live      # 실전(명시적 확인 필요)
  python -m aitrader run --mode manual        # 주문 보조: 봇이 주문을 제안하고 사람이 증권사 앱에서 체결
  python -m aitrader run --market krx ...     # 국내 주식
  python -m aitrader backtest --days 20       # 합성 시세 백테스트
  python -m aitrader research --dir <폴더>     # 실제 과거 시세(야후 형식 JSON)로 백테스트
  python -m aitrader bench                    # 판단 지연(틱→결정) 측정
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import signal
import sys
import time

from .config import Config, load_config

LIVE_CONFIRM = "I_UNDERSTAND_REAL_MONEY"


def make_broker(cfg: Config):
    if cfg.mode == "sim":
        from .brokers.sim import PaperBroker
        return PaperBroker(cfg)
    if cfg.mode in ("kis-mock", "kis-live"):
        cfg.kis.env = "real" if cfg.mode == "kis-live" else "mock"
        if cfg.market == "US":
            from .brokers.kis_overseas import KISOverseasBroker
            return KISOverseasBroker(cfg)
        from .brokers.kis import KISBroker
        return KISBroker(cfg)
    if cfg.mode == "manual":
        from .brokers.manual import ManualBroker
        return ManualBroker(cfg)
    if cfg.mode.startswith("samsung"):
        raise SystemExit(
            "삼성증권은 현재 개인 고객용 매매 Open API를 공개하지 않아 자동 주문을 연결할 수 없습니다.\n"
            "삼성증권 계좌로 쓰려면 --mode manual (주문 보조: 봇이 주문 내용을 알려주고 직접 체결)을,\n"
            "무인 자동매매는 --mode kis-mock / kis-live (한국투자증권)를 사용하세요.")
    raise SystemExit(f"알 수 없는 mode: {cfg.mode}")


async def _run(cfg: Config, dashboard: bool) -> None:
    from .supervisor import Supervisor
    sup = Supervisor(cfg, make_broker(cfg))
    dash = None
    if dashboard:
        from .dashboard.server import Dashboard
        d = cfg.dashboard
        dash = Dashboard(sup, d.host, d.port, d.token)
        await dash.start()
        print(f"대시보드: http://{d.host}:{d.port}/  (mode={cfg.mode}, market={cfg.market})", flush=True)
    # 지연 튐 방지: 시작 시점 객체는 GC 대상에서 빼고, 수집 주기를 늘리고,
    # 일지 스레드가 GIL을 오래 쥐지 못하게 전환 간격을 줄인다.
    gc.collect()
    gc.freeze()
    gc.set_threshold(100_000, 50, 100)
    sys.setswitchinterval(0.0005)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, sup.stop)
        except NotImplementedError:                    # Windows
            pass
    try:
        await sup.run()
    finally:
        if dash:
            await dash.stop()
    e = sup.engine
    m = cfg.mkt
    print(f"종료 — 자산 {m.money(e.equity, signed=False)}, 누적 실현손익 {m.money(e.realized_total)}, "
          f"거래 {len(e.trades)}건, 전략 v{e.p.version}", flush=True)


def _loop_run(coro) -> None:
    try:
        import uvloop                                  # libuv 기반 이벤트 루프 — 기본 asyncio보다 빠르다
        uvloop.run(coro)
    except ImportError:
        asyncio.run(coro)


def _cfg(a) -> Config:
    return load_config(a.config, getattr(a, "market", None), getattr(a, "style", None))


def cmd_run(a) -> None:
    cfg = _cfg(a)
    if a.mode:
        cfg.mode = a.mode
    if a.speed is not None:
        cfg.sim.speed = a.speed
    if a.days is not None:
        cfg.sim.days = a.days
    if a.port is not None:
        cfg.dashboard.port = a.port
    if a.host:
        cfg.dashboard.host = a.host
    if cfg.mode == "sim" and not a.llm:
        cfg.llm.enabled = False        # 배속 시뮬레이션이 실제 API 비용을 태우지 않도록 기본은 끔
    if cfg.mode == "kis-live" and os.environ.get("AITRADER_LIVE_CONFIRM") != LIVE_CONFIRM:
        raise SystemExit(
            "실전 매매는 실제 돈이 움직입니다. 모의투자(kis-mock)로 충분히 검증한 뒤,\n"
            f"환경변수 AITRADER_LIVE_CONFIRM={LIVE_CONFIRM} 을 설정해야 시작됩니다.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _loop_run(_run(cfg, not a.no_dashboard))


def _report(tag: str, r: dict, e, m, el: float) -> None:
    print(tag)
    print(f"거래 {r['n']}건 · 손익 {m.money(r['pnl'])} ({r['ret']:+.2%}) · 승률 {r['win_rate']:.0%} · "
          f"손익비 {r['pf']} · 기대값 {r['exp_r']:+.2f}R · 최대낙폭 {m.money(r['max_dd'], signed=False)} · "
          f"비용 {m.money(e.fees_total, signed=False)}")
    print(f"틱 {e.ticks:,}개 처리 {el:.2f}초 · 판단 지연(µs) {e.lat.summary(1000.0)}")


def cmd_backtest(a) -> None:
    from .backtest import Backtester
    from .brokers.sim import SimMarket
    from .params import ParamStore
    cfg = _cfg(a)
    mk = SimMarket(cfg.universe[:cfg.max_symbols], a.seed, cfg.sim.tick_sec, etfs=frozenset(cfg.etfs),
                   market=cfg.mkt, open_hhmm=cfg.session.open, close_hhmm=cfg.session.close)
    bt = Backtester(cfg, ParamStore(None, cfg.params).current)
    t0 = time.perf_counter()
    for _ in range(a.days):
        for sym in mk.symbols:
            bt.engine.set_daily(sym, mk.history(sym, 60))
        bt.run_ticks(mk.day_ticks())
        bt.engine.flush_bars()
        mk.next_day()
    _report(f"[합성 시세 {a.days}일, seed={a.seed}, {cfg.market}] — 실전 수익성과 무관한 동작 검증용",
            bt.result(), bt.engine, cfg.mkt, time.perf_counter() - t0)


def cmd_research(a) -> None:
    from .research import run_research
    cfg = _cfg(a)
    run_research(cfg, a.dir, a.interval or ("1d_5y" if cfg.style == "swing" else "5m_60d"),
                 commission=a.commission, learn=a.learn, no_halt=a.no_halt, start=a.start, end=a.end)


def cmd_bench(a) -> None:
    from .backtest import Backtester
    from .brokers.sim import SimMarket
    from .params import ParamStore
    cfg = _cfg(a)
    mk = SimMarket(cfg.universe[:cfg.max_symbols], 7, cfg.sim.tick_sec, etfs=frozenset(cfg.etfs),
                   market=cfg.mkt, open_hhmm=cfg.session.open, close_hhmm=cfg.session.close)
    ticks = []
    for _ in range(3):
        ticks += list(mk.day_ticks())
        mk.next_day()
    bt = Backtester(cfg, ParamStore(None, cfg.params).current)
    t0 = time.perf_counter()
    bt.run_ticks(ticks)
    el = time.perf_counter() - t0
    s = bt.engine.lat.summary(1000.0)
    print(f"틱 {len(ticks):,}개 · 초당 {len(ticks) / el:,.0f}틱 처리")
    print(f"틱→주문결정 지연: 중앙값 {s['p50']}µs · 99% {s['p99']}µs · 최대 {s['max']}µs")


def main() -> None:
    ap = argparse.ArgumentParser(prog="aitrader", description="AI 자동매매 시스템")
    ap.add_argument("--config", default=None, help="config.toml 경로")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--market", choices=["us", "krx", "US", "KRX"], help="시장(기본: config.toml 또는 us)")
        p.add_argument("--style", choices=["swing", "intraday"], help="매매 방식(미국 기본: swing)")

    r = sub.add_parser("run", help="매매 실행 + 대시보드")
    common(r)
    r.add_argument("--mode", choices=["sim", "kis-mock", "kis-live", "manual", "samsung"])
    r.add_argument("--speed", type=float, help="시뮬레이션 배속")
    r.add_argument("--days", type=int, help="시뮬레이션 일수(0=무한)")
    r.add_argument("--port", type=int)
    r.add_argument("--host")
    r.add_argument("--no-dashboard", action="store_true")
    r.add_argument("--llm", action="store_true", help="시뮬레이션에서도 Claude 호출 사용")
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("backtest", help="합성 시세 백테스트")
    common(b)
    b.add_argument("--days", type=int, default=20)
    b.add_argument("--seed", type=int, default=7)
    b.set_defaults(fn=cmd_backtest)
    rs = sub.add_parser("research", help="실제 과거 시세로 백테스트")
    common(rs)
    rs.add_argument("--dir", required=True, help="<종목>_<간격>.json 파일이 있는 폴더(야후 차트 형식)")
    rs.add_argument("--interval", default=None, help="파일 이름의 간격 부분(예: 1d_5y, 5m_60d)")
    rs.add_argument("--commission", type=float, default=None, help="수수료율 덮어쓰기(예: 0.0007)")
    rs.add_argument("--learn", action="store_true", help="날마다 복기·학습을 돌리며 검증(규칙 기반)")
    rs.add_argument("--no-halt", action="store_true", help="손실 한도 정지를 끄고 전 기간을 본다")
    rs.add_argument("--start", help="시작일 YYYY-MM-DD")
    rs.add_argument("--end", help="종료일 YYYY-MM-DD(포함 안 함)")
    rs.set_defaults(fn=cmd_research)
    c = sub.add_parser("bench", help="판단 지연 측정")
    common(c)
    c.set_defaults(fn=cmd_bench)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
