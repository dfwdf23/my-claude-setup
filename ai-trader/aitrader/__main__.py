"""실행 진입점.

  python -m aitrader run                      # 시뮬레이션(합성 시세) + 대시보드
  python -m aitrader run --mode kis-mock      # 한국투자증권 모의투자
  python -m aitrader run --mode kis-live      # 실전(명시적 확인 필요)
  python -m aitrader backtest --days 20       # 합성 시세 백테스트
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
        from .brokers.kis import KISBroker
        cfg.kis.env = "real" if cfg.mode == "kis-live" else "mock"
        return KISBroker(cfg)
    if cfg.mode.startswith("samsung"):
        raise SystemExit(
            "삼성증권은 현재 개인 고객용 매매 Open API를 공개하지 않아 직접 연동할 수 없습니다.\n"
            "API가 열리면 aitrader/brokers/base.py 의 Broker 를 구현해 붙이면 됩니다.\n"
            "지금은 --mode kis-mock (한국투자증권 모의투자) 또는 --mode sim 을 사용하세요.")
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
        print(f"대시보드: http://{d.host}:{d.port}/  (mode={cfg.mode})", flush=True)
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
    print(f"종료 — 자산 {e.equity:,.0f}원, 누적 실현손익 {e.realized_total:+,.0f}원, 거래 {len(e.trades)}건, "
          f"전략 v{e.p.version}", flush=True)


def _loop_run(coro) -> None:
    try:
        import uvloop                                  # libuv 기반 이벤트 루프 — 기본 asyncio보다 빠르다
        uvloop.run(coro)
    except ImportError:
        asyncio.run(coro)


def cmd_run(a) -> None:
    cfg = load_config(a.config)
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


def cmd_backtest(a) -> None:
    from .backtest import Backtester
    from .brokers.sim import SimMarket
    from .params import ParamStore
    cfg = load_config(a.config)
    mk = SimMarket(cfg.universe[:cfg.max_symbols], a.seed, cfg.sim.tick_sec, etfs=frozenset(cfg.etfs))
    bt = Backtester(cfg, ParamStore(None).current)
    t0 = time.perf_counter()
    for _ in range(a.days):
        bt.run_ticks(mk.day_ticks())
        bt.engine.flush_bars()
        mk.next_day()
    r = bt.result()
    e = bt.engine
    print(f"[합성 시세 {a.days}일, seed={a.seed}] — 실전 수익성과 무관한 동작 검증용")
    print(f"거래 {r['n']}건 · 손익 {r['pnl']:+,}원 ({r['ret']:+.2%}) · 승률 {r['win_rate']:.0%} · "
          f"손익비 {r['pf']} · 기대값 {r['exp_r']:+.2f}R · 최대낙폭 {r['max_dd']:,}원 · 비용 {e.fees_total:,.0f}원")
    print(f"틱 {e.ticks:,}개 처리 {time.perf_counter() - t0:.2f}초 · 판단 지연(µs) {e.lat.summary(1000.0)}")


def cmd_bench(a) -> None:
    from .backtest import Backtester
    from .brokers.sim import SimMarket
    from .params import ParamStore
    cfg = load_config(a.config)
    mk = SimMarket(cfg.universe[:cfg.max_symbols], 7, cfg.sim.tick_sec, etfs=frozenset(cfg.etfs))
    ticks = []
    for _ in range(3):
        ticks += list(mk.day_ticks())
        mk.next_day()
    bt = Backtester(cfg, ParamStore(None).current)
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
    r = sub.add_parser("run", help="매매 실행 + 대시보드")
    r.add_argument("--mode", choices=["sim", "kis-mock", "kis-live", "samsung"])
    r.add_argument("--speed", type=float, help="시뮬레이션 배속")
    r.add_argument("--days", type=int, help="시뮬레이션 일수(0=무한)")
    r.add_argument("--port", type=int)
    r.add_argument("--host")
    r.add_argument("--no-dashboard", action="store_true")
    r.add_argument("--llm", action="store_true", help="시뮬레이션에서도 Claude 호출 사용")
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("backtest", help="합성 시세 백테스트")
    b.add_argument("--days", type=int, default=20)
    b.add_argument("--seed", type=int, default=7)
    b.set_defaults(fn=cmd_backtest)
    c = sub.add_parser("bench", help="판단 지연 측정")
    c.set_defaults(fn=cmd_bench)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
