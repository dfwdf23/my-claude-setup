"""실행 진입점.

  python -m aitrader run                      # 시뮬레이션(합성 시세, 미국 시장) + 대시보드
  python -m aitrader run --mode kis-mock      # 한국투자증권 모의투자(해외주식)
  python -m aitrader run --mode kis-live      # 실전(명시적 확인 필요)
  python -m aitrader run --mode manual        # 주문 보조: 봇이 주문을 제안하고 사람이 증권사 앱에서 체결
  python -m aitrader run --market krx ...     # 국내 주식
  python -m aitrader backtest --days 20       # 합성 시세 백테스트
  python -m aitrader fetch --dir <폴더>        # 과거 시세 내려받기(야후 파이낸스)
  python -m aitrader research --dir <폴더>     # 내려받은 실제 과거 시세로 백테스트
  python -m aitrader bench                    # 판단 지연(틱→결정) 측정
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
from pathlib import Path
import signal
import sys
import time
import tomllib

from .brokers.base import BrokerError
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
    except ImportError:                                # 윈도우에는 uvloop 이 없다 → 기본 이벤트 루프(오류가 아니다)
        uvloop = None
    # 실행은 except 블록 밖에서 한다 — 안에서 돌리면, 나중에 난 오류 앞에 'uvloop 없음' 추적이 덧붙어 원인처럼 보인다
    if uvloop is not None:
        uvloop.run(coro)
    else:
        asyncio.run(coro)


BROKER_HINTS = {
    "EGW02007": "실전용 앱키를 모의투자 모드(kis-mock)에 넣었습니다. KIS Developers 에서 '모의투자계좌'로 API 를 따로 신청해 "
                "받은 앱키·시크릿과 모의투자 계좌번호를 .env 에 넣으세요. (실전으로 켜려는 것이라면 mode 를 kis-live 로 — "
                "다만 모의투자로 먼저 확인하기를 권합니다)",
    "EGW02006": "모의투자용 앱키를 실전 모드(kis-live)에 넣었습니다. 실전용 앱키·계좌번호로 바꾸거나 mode 를 kis-mock 으로 두세요.",
    "EGW00103": "앱키가 유효하지 않습니다. .env 의 KIS_APP_KEY 를 다시 복사해 넣으세요(앞뒤 공백·따옴표 없이).",
    "EGW00105": "앱 시크릿이 유효하지 않습니다. .env 의 KIS_APP_SECRET 을 다시 복사해 넣으세요(한 줄로, 앞뒤 공백 없이).",
}


def broker_hint(msg: str) -> str:
    """증권사가 연결을 거부했을 때, 흔한 원인을 사람이 알아볼 말로 덧붙인다."""
    for code, hint in BROKER_HINTS.items():
        if code in msg:
            return hint
    if "토큰 발급 실패" in msg:
        return ".env 의 KIS_APP_KEY·KIS_APP_SECRET 이 맞는지, 모드(모의/실전)와 같은 종류의 키인지 확인하세요."
    if "계좌" in msg:
        return ".env 의 KIS_ACCOUNT(계좌번호 앞 8자리)·KIS_PRODUCT(뒤 2자리)가 그 키를 발급받은 계좌와 같은지 확인하세요."
    return "키·계좌번호가 모드(모의/실전)와 맞는지 .env 를 확인하세요."


def _cfg(a) -> Config:
    try:
        return load_config(a.config, getattr(a, "market", None), getattr(a, "style", None))
    except FileNotFoundError as e:
        raise SystemExit(f"설정 파일을 찾을 수 없습니다: {e}")
    except (ValueError, tomllib.TOMLDecodeError) as e:      # 긴 오류 추적 대신 무엇이 틀렸는지 한 줄로
        raise SystemExit(f"설정 오류: {e}")


def _warm(bt, mk, cfg: Config) -> None:
    """일봉 방식은 하루에 봉이 하나라, 과거 일봉으로 지표를 미리 채우지 않으면 한 달 가까이 판단이 나오지 않는다.
    실제 실행(총괄의 장전 준비)과 같은 방법으로 예열한다."""
    if cfg.strategy.bar_sec >= 86_400:
        for sym in mk.symbols:
            bt.engine.warm(sym, mk.history(sym, 3 * cfg.strategy.warmup_bars))


MODE_KO = {"sim": "연습(합성 시세 — 실제 계좌와 무관)", "manual": "주문 보조(실제 시세, 주문은 사람이 직접)",
           "kis-mock": "한국투자증권 모의투자", "kis-live": "한국투자증권 실전(실제 돈)"}


def startup_notes(config_arg: str | None, mode: str, cwd: Path | None = None) -> list[str]:
    """시작할 때 '어느 설정 파일을 읽었고 어떤 모드로 켜지는지'를 알려 주는 안내문.
    설정 파일이 없으면 조용히 연습 모드로 켜지므로, 실전으로 바꿨다고 생각했는데 연습 화면이 뜨는 일을 막는다."""
    cwd = cwd or Path.cwd()
    cfg_path = Path(config_arg) if config_arg else cwd / "config.toml"
    out = [f"실행 폴더: {cwd}"]
    if cfg_path.exists():
        out.append(f"설정 파일: {cfg_path.name} 을 읽었습니다")
    else:
        out.append(f"설정 파일: {cfg_path.name} 이 이 폴더에 없습니다 → 기본값으로 시작합니다")
        for wrong, why in (("config.toml.txt", "이름 끝에 .txt 가 붙어 있습니다. 탐색기 [보기 → 파일 확장명]을 켜고 .txt 를 지우세요"),
                           ("config.example.toml", "이 파일은 견본입니다. 복사해서 이름을 config.toml 로 바꾼 뒤 그 파일을 고치세요")):
            if (cwd / wrong).exists():
                out.append(f"  ! {wrong} 은 있습니다 — {why}")
    if (cwd / ".env").exists():
        out.append("키 파일: .env 를 읽었습니다")
    else:
        out.append("키 파일: .env 가 이 폴더에 없습니다(증권사 키·알림 설정이 적용되지 않습니다)")
        for wrong in (".env.txt", "env", "env.txt"):
            if (cwd / wrong).exists():
                out.append(f"  ! {wrong} 은 있습니다 — 파일 이름이 정확히 .env 여야 합니다(앞에 점, 뒤에 .txt 없음)")
    out.append(f"모드: {mode} — {MODE_KO.get(mode, mode)}")
    return out


def cmd_run(a) -> None:
    cfg = _cfg(a)
    if a.mode:
        cfg.mode = a.mode
    print("\n".join(startup_notes(a.config, cfg.mode)) + "\n", flush=True)
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
    try:
        _loop_run(_run(cfg, not a.no_dashboard))
    except BrokerError as e:                           # 긴 오류 추적 대신 무엇이 틀렸는지 알려 주고 끝낸다
        raise SystemExit(f"\n증권사 연결 실패: {e}\n→ {broker_hint(str(e))}") from None


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
    _warm(bt, mk, cfg)
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


def cmd_fetch(a) -> None:
    from .research import fetch_history
    cfg = _cfg(a)
    syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    print(f"과거 시세를 내려받습니다 → {a.dir}  (야후 파이낸스 비공식 조회, 개인적 조사 용도)")
    saved = fetch_history(cfg, a.dir, a.interval, a.rng, syms)
    if not saved:
        raise SystemExit("내려받은 파일이 없습니다 — 네트워크 연결이나 종목 코드를 확인하세요")
    print(f"완료: 파일 {len(saved)}개. 이어서  python -m aitrader research --dir {a.dir}  로 검증할 수 있습니다.")


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
    _warm(bt, mk, cfg)
    t0 = time.perf_counter()
    bt.run_ticks(ticks)
    el = time.perf_counter() - t0
    e = bt.engine
    s = e.lat.summary(1000.0)
    print(f"틱 {len(ticks):,}개 · 초당 {len(ticks) / el:,.0f}틱 처리")
    print(f"틱→주문결정 지연: 중앙값 {s['p50']}µs · 99% {s['p99']}µs · 최대 {s['max']}µs")
    print(f"(그중 매수 판단까지 간 틱 {e.chart.calls:,}회 · 매도 검사 {e.seller.calls:,}회 — "
          f"{'일봉 방식은 판단이 하루 한 번이라 대부분은 시세 반영·매도 검사 시간입니다' if cfg.strategy.bar_sec >= 86_400 else '분봉 방식'})")


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
    ft = sub.add_parser("fetch", help="과거 시세 내려받기(research 의 입력)")
    common(ft)
    ft.add_argument("--dir", required=True, help="저장할 폴더")
    ft.add_argument("--interval", default=None, help="봉 간격(기본: 스윙 1d, 단타 5m)")
    ft.add_argument("--range", dest="rng", default=None, help="기간(기본: 스윙 5y, 단타 60d)")
    ft.add_argument("--symbols", default=None, help="쉼표로 구분한 종목(기본: 설정의 감시 후보)")
    ft.set_defaults(fn=cmd_fetch)
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
    for st in (sys.stdout, sys.stderr):             # 콘솔 글자 인코딩(윈도우 등) 때문에 출력이 죽지 않게
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
