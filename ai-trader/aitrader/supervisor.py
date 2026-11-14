"""총괄 에이전트 — 모든 에이전트를 묶고, 사람이 없어도 하루 일과가 저절로 돌게 한다.

  장전      종목선정 → 일봉 맥락·지표 예열 → 시세 구독
  장중      틱마다 엔진(빠른 경로) · 매분 시장분석 · 소식 수집 · 주문 시간초과 정리 · 시세 끊김 감시 · 계좌 대사
  장후      일지 기록 → 복기(손실 원인 분석·검증·파라미터 갱신) → 다음 거래일 대기
모든 시각은 거래소 현지 시각(cfg.mkt) 기준이다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

from .agents.base import Agent
from .agents.learner import Learner
from .agents.market import MarketAnalyst, Screener
from .agents.news import NewsAgent
from .brokers.base import Broker
from .config import Config
from .engine import Engine
from .executor import Executor
from .journal import Journal
from .llm import LLM
from .metrics import summarize
from .models import EXIT_SESSIONS, SELL, Fill, Position, Trade, reason_ko
from .notify import Notifier
from .params import ParamStore

log = logging.getLogger("aitrader")
_POS_FIELDS = {f.name for f in fields(Position)}


def store_dir(cfg: Config) -> Path:
    """모드·시장·방식별 저장 폴더. 방식이 다르면 학습된 전략 파라미터와 기록 봉(분봉/일봉)이 전혀 다른 것이므로 섞지 않는다."""
    name = cfg.mode if cfg.market == "KRX" else f"{cfg.mode}-{cfg.market.lower()}"
    default_style = "intraday" if cfg.market == "KRX" else "swing"
    if cfg.style != default_style:
        name += f"-{cfg.style}"
    return Path(cfg.data_dir) / name


class Supervisor(Agent):
    name = "supervisor"
    role = "총괄"
    kind = "slow"

    def __init__(self, cfg: Config, broker: Broker, persist: bool = True) -> None:
        super().__init__()
        self.cfg = cfg
        self.broker = broker
        self.m = cfg.mkt
        self.live = cfg.mode in ("kis-mock", "kis-live")      # 실제 증권사 계좌가 있는 모드
        self.manual = cfg.mode == "manual"                    # 주문 보조(사람이 체결)
        d = store_dir(cfg) if persist else None
        self.dir = d
        self.journal = Journal(d / "journal.db" if d else None)
        self.store = ParamStore(d / "params.json" if d else None, cfg.params)
        self.llm = LLM(cfg.llm, self.journal)
        self.engine = Engine(cfg, self.store.current, cfg.capital)
        # config.toml 의 [params] 는 '처음 시작할 때의 기본값'이다. 저장된 전략이 이미 있으면 그쪽이 우선이므로,
        # 사용자가 적은 값이 적용되지 않았다는 것을 조용히 넘기지 않고 알려 준다.
        ignored = {k: v for k, v in cfg.user_params.items() if getattr(self.store.current, k, v) != v}
        if ignored and len(self.store.history) > 0 and d is not None and (d / "params.json").exists():
            self.engine.emit("system", "", "config.toml 의 [params] " + ", ".join(f"{k}={v}" for k, v in ignored.items())
                             + f" 는 적용되지 않았습니다 — 저장된 전략(v{self.store.current.version})이 우선입니다. "
                             f"적용하려면 {d / 'params.json'} 을 지우고 다시 시작하세요.")
        timeout = cfg.manual.ticket_ttl_min * 60.0 if self.manual else cfg.risk.order_timeout_sec
        e0 = self.engine
        realtime = cfg.mode in ("kis-mock", "kis-live") or self.manual
        self.executor = Executor(broker, e0, timeout,
                                 clock=(lambda: max(e0.now, time.time())) if realtime else broker.now)
        if self.manual:                                       # 건너뛰거나 만료된 제안은 한동안 다시 띄우지 않는다
            e0.retry_sec = cfg.manual.snooze_min * 60.0
        self.market = MarketAnalyst(cfg, self.llm)
        self.daily = cfg.strategy.bar_sec >= 86_400          # 일봉 방식(스윙)
        self.screener = Screener(cfg.learn.lookback_days + 70 if self.daily else 60)
        self.learner = Learner(cfg, self.store, self.llm, self.journal)
        self.news = (NewsAgent(cfg, self.llm, self.engine, broker, self.journal)
                     if cfg.news.enabled and cfg.mode != "sim" else None)
        self.notifier = Notifier(cfg.notify)
        self.agents: list[Agent] = [self, self.market, self.screener] + ([self.news] if self.news else []) + [
            *self.engine.agents, self.executor, self.learner]
        e = self.engine
        e.on_bar_cb = self.journal.bar
        e.on_fill_cb = self._on_fill
        e.on_trade_cb = self._on_trade
        self.keep_state = self.live or self.manual            # 재시작 후 이어받아야 하는 모드
        if self.keep_state:
            self.executor.on_change = self._save_state
        if self.manual and hasattr(broker, "on_ticket"):
            broker.on_ticket = self._on_ticket
            broker.on_late = self._on_late_fill
        broker.on_tick = e.on_tick                     # 큐 없이 직결: 수신 루프 → 엔진
        broker.on_day_start = self.day_start
        broker.on_day_end = self.day_end
        broker.on_status = lambda msg: e.emit("system", "", msg)
        e.clock = broker.now
        self.phase = "INIT"
        self.today: date | None = None
        self.symbols: list[str] = []
        self.days: list[dict] = []
        if d:                                         # 재시작해도 일별 기록·연속 손실일·'변경 후 지난 날 수'가 이어지게
            self.days = [{"day": r["day"], "equity": r["equity"], "pnl": r["pnl"], "n": r["n"], "wins": r["wins"],
                          "version": r["pv"], "adopted": ""} for r in self.journal.load_daily(120)]
            self.learner.day_pnls = [x["pnl"] for x in self.days[-5:]]
            if len(self.store.history) > 1:
                changed = str(self.m.local(float(self.store.history[-1].get("ts", 0.0))).date())
                self.learner.days_since_change = sum(1 for x in self.days if x["day"] > changed)
        self.history: list[Trade] = self.journal.load_trades(time.time() - 60 * 86400) if d else []
        self.started = time.time()
        self.stale_sec = max(60.0, cfg.manual.poll_sec * 4) if self.manual else cfg.risk.stale_feed_sec
        self._stop = asyncio.Event()
        self._mkt_ts = 0.0
        self._mkt_busy = False
        self._first_mkt = True
        self._tick_mark = (0, time.monotonic())
        self._last_tick_wall = time.monotonic()
        self.tps = 0.0
        self._recon_ts = 0.0
        self._recon_prev: dict[str, int] = {}
        self._acct_extra: dict[str, int] = {}         # 종목 → 계좌에 장부보다 더 있는 수량(이미 알린 값. 달라질 때만 다시 알린다)
        self._save_ts = 0.0
        self._save_failed = False
        self._warmed: set[str] = set()                # 일봉 이력으로 지표를 예열한 종목
        self._alert_seq = 0                           # 텔레그램으로 넘긴 마지막 이벤트 번호
        self._clock_ts = 0.0
        self._clock_bad = False
        self._day_started: date | None = None
        self._day_ended: date | None = None

    # ------------------------------------------------------------------ 실행
    async def run(self) -> None:
        await self.broker.start()
        if self.broker.has_account:
            await self.reconcile(initial=True)
        elif self.manual:
            await self._restore(None)                   # 계좌 조회가 없으므로 저장된 장부를 그대로 이어받는다
        tasks = [asyncio.create_task(self.broker.run(), name="broker"),
                 asyncio.create_task(self._timer_loop(), name="timer"),
                 asyncio.create_task(self._stop.wait(), name="stop")]
        if not self.broker.drives_clock:
            tasks.append(asyncio.create_task(self._scheduler(), name="scheduler"))
        if self.news is not None:
            tasks.append(asyncio.create_task(self._aux("정보수집", self.news.run), name="news"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.get_name() != "stop" and t.exception() is not None:
                    log.error("작업 %s 오류로 종료: %r", t.get_name(), t.exception())
                    n = len(self.engine.positions)
                    self.notifier.send(f"⛔ 자동매매 프로그램이 오류로 멈춥니다({t.get_name()}: {t.exception()!r})."
                                       + (f" 보유 종목 {n}개는 더 이상 감시되지 않습니다 — 직접 확인하세요." if n else ""))
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.live:                               # 걸려 있는 주문을 남긴 채 꺼지지 않는다
                try:
                    await asyncio.wait_for(self.executor.cancel_all(), 8.0)
                except Exception:
                    pass
            self.executor.shutdown()                    # 확정 못 한 주문은 저장해 두고 다음 시작 때 정리한다
            await self.broker.stop()
            self._save_state()
            await self.notifier.close()
            self.journal.close()

    def stop(self) -> None:
        self._stop.set()

    async def _aux(self, name: str, fn) -> None:
        """보조 작업(소식 수집 등)을 돌린다. 오류로 끝나도 매매는 계속하고, 잠시 뒤 그 작업만 다시 시작한다."""
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.exception("%s 작업 오류 — 30초 뒤 다시 시작", name)
                self.engine.emit("system", "", f"{name} 오류(매매는 계속, 30초 뒤 다시 시작): {ex!r}"[:160])
                await asyncio.sleep(30.0)

    # ------------------------------------------------------------------ 하루 일과
    async def day_start(self, d: date) -> None:
        self.phase = "PRE"
        self.today = d
        self._day_started = d
        now = self.broker.now()
        self.llm.new_day(time.strftime("%Y-%m-%d"))                   # 토큰 예산은 실제 날짜 기준
        self.market.new_day()
        self._first_mkt = True
        syms = await self.screener.select(self.broker, self.cfg.universe, self.cfg.max_symbols, now)
        e = self.engine
        syms = list(dict.fromkeys(syms + list(e.positions)))   # 보유 중인 종목은 반드시 시세를 받아야 한다
        self.symbols = syms
        for sym in list(e.positions):                 # 보유 종목의 일봉은 꼭 필요하다(전일 종가 평가·추세 청산) → 한 번 더 받아 본다
            if not self.screener.bars.get(sym):
                for _ in range(2):
                    await asyncio.sleep(1.0)
                    self.screener.bars[sym] = await self.screener.fetch(self.broker, sym)
                    if self.screener.bars[sym]:
                        break
        day0 = self.m.epoch(d)
        ses = self.cfg.session
        early = self.m.local(now).hour * 3600 + self.m.local(now).minute * 60 < ses.tod("open") + 1800
        today_n = self.m.local_day(now)
        # 오늘 아침에 일봉을 실제로 받은 종목 → (가장 최근 일봉의 날짜, 그 종가)
        fresh: dict[str, tuple[int, float]] = {}
        n_warm = 3 * self.cfg.strategy.warmup_bars
        hist: dict[str, list] = {}
        for sym in syms:                              # 1) 일봉 맥락(전일 종가·고가, 평균 거래량, 일봉 추세)
            e.symbol_state(sym)
            allb = self.screener.bars.get(sym, [])
            dbars = hist[sym] = [b for b in allb if b.ts < day0]                     # 어제까지의 일봉
            if dbars and dbars[-1].close > 0:
                fresh[sym] = (self.m.local_day(dbars[-1].ts), dbars[-1].close)
            e.set_daily(sym, dbars[-60:])
        # 2) 보유 종목을 전일 종가로 평가한다. 반드시 3)·4)보다 먼저 — 오늘 봉을 채우는 seed_bar 가 엔진의 날짜를
        #    오늘로 넘기면서 '오늘 손익의 기준 자산'을 그 순간의 평가액으로 잡기 때문이다. 순서가 바뀌면 장중에 켰을 때
        #    꺼져 있던 며칠의 가격 변화가 통째로 오늘 손익이 된다(일일 손실 한도 오발동, 반대 방향이면 한도 무력화).
        self._mark_at_prev_close(now, fresh)
        for sym in syms:                              # 3) 지표 예열: 어제까지의 봉으로 미리 채운다
            s = e.sym[sym]
            dbars = hist[sym]
            if self.daily:
                # '이력으로 예열했는가'를 따로 기억한다 — 첫날 조회가 실패했다고 그 종목이 한 달 가까이 준비 안 된 채로
                # 남지 않게, 받을 수 있게 된 날 채운다. 또 엔진이 가진 마지막 봉이 증권사의 최근 일봉보다 오래됐으면
                # (감시 목록에서 빠졌다 돌아온 종목, 시세가 없던 날, 마감 처리를 못 하고 넘어간 날) 중간이 빈 채로
                # 이어 붙이지 않고 처음부터 다시 채운다. 다시 채운 종목에 남아 있던 '판단 대기' 표시는 믿지 않는다.
                have = self.m.local_day(s.hist[-1][0]) if s.hist else -1
                if dbars and (sym not in self._warmed or (sym in fresh and have < fresh[sym][0])):
                    e.warm(sym, dbars[-n_warm:])
                    self._warmed.add(sym)
                    # 어제 봉에 대한 판단을 오늘 할 것인가:
                    #  · 오늘 이미 판단을 마쳤으면(같은 날 다시 켠 경우) 다시 하지 않는다 — 보유 일수가 두 번 세어진다.
                    #  · 장 초반에 켜졌으면 한다(새 매수 판단 포함).
                    #  · 늦게 켜졌으면 새 매수는 건너뛰되, 보유 종목의 청산 판단(추세 이탈·보유 기간)은 한다.
                    s.undecided = e.decided.get(sym) != today_n and (early or sym in e.positions)
            elif s.ind.n == 0:
                e.warm(sym, self.journal.recent_bars(sym, n_warm))
        if self.daily:                                # 4) 장중에 켜졌다 → 오늘 봉의 지나간 구간을 채운다
            for sym in syms:
                allb = self.screener.bars.get(sym, [])
                if allb and allb[-1].ts >= day0:
                    e.seed_bar(sym, allb[-1], now)
        lost = [x for x in syms if x in self.screener.failed]
        if lost and self.daily:
            held = [x for x in lost if x in e.positions]
            cold = [x for x in lost if x not in self._warmed]
            msg = (f"일봉을 받지 못한 종목: {', '.join(lost)} — 일봉 맥락(추세·평균 거래량)은 지난번 값 그대로 씁니다"
                   + (f". {', '.join(cold)} 는 지표가 준비되지 않아 오늘 새 매수 판단을 하지 않습니다" if cold else "")
                   + (f". 보유 중인 {', '.join(held)} 는 전일 종가 기준 평가를 건너뛰고 손절·익절만 감시합니다" if held else ""))
            e.emit("risk" if held else "system", "", msg, sent=1)
            if held:
                self.notifier.send("⚠ " + msg)
        if e.view_on:
            await self._backfill_view(syms, now)
        if self.news is not None:
            self.news.new_day()
            self.news.set_symbols(syms)
        await self.broker.subscribe(syms)
        e.set_params(self.store.current)
        self.phase = "OPEN"
        self._last_tick_wall = time.monotonic()
        self.note(f"{d} 장 시작 — {len(syms)}종목 감시 · 전략 v{e.p.version}", now)
        e.emit("system", "", f"{d} 장 시작 — 감시 {len(syms)}종목, 전략 v{e.p.version}")

    def _mark_at_prev_close(self, now: float, fresh: dict[str, tuple[int, float]]) -> None:
        """오늘 시세를 아직 받지 않았다면 보유 종목을 '전일 종가'로 평가하고, 그 자산을 오늘 손익의 기준으로 삼는다.

        며칠 꺼져 있다가 켜지면 장부의 마지막 가격은 꺼지던 때의 값이다. 그대로 두면 그 사이의 가격 변화가
        전부 '오늘 하루의 손실'로 계산돼 일일 손실 한도(전량 청산)가 잘못 발동한다.

        fresh: 오늘 아침 일봉 조회에 성공한 종목 → (그 가장 최근 일봉의 날짜, 종가). 조회에 실패한 종목은 묵은 값밖에
        없으므로 건드리지 않는다. 또 엔진이 이미 그날의 시세를 본 종목(계속 켜져 있던 경우)은 마지막 틱이 곧 그날
        종가이므로 건드리지 않는다.

        엔진의 날짜(e.day)가 오늘로 넘어가기 전에 불러야 한다(day_start 의 순서 참고)."""
        e = self.engine
        if not e.positions or e.day == self.m.local_day(now):
            return                                    # 보유 없음, 또는 오늘 시세를 이미 받는 중(같은 날 재시작 포함)
        moved = False
        for sym in e.positions:
            s = e.sym[sym]
            bar_day, close = fresh.get(sym, (None, 0.0))
            if bar_day is None or bar_day <= e.day:
                continue
            if close > 0 and close != s.last:
                s.last = close
                moved = True
        if moved:
            eq = e.cash + sum(p.qty * e.sym[p.symbol].last for p in e.positions.values())
            e.day_start_equity = eq
            e.risk.start_equity = eq
            e.mark()

    async def _backfill_view(self, syms: list[str], now: float) -> None:
        """장중에 켜졌을 때 이미 지나간 분봉을 채워 실시간 차트가 비어 보이지 않게 한다(화면 전용, 매매 판단과 무관)."""
        e = self.engine
        sec = int(e.view_sec)
        cur = now - now % sec                             # 진행 중인 봉은 틱으로 직접 만든다
        for sym in syms:
            s = e.symbol_state(sym)
            if s.view:
                continue
            try:
                bars = await self.broker.get_intraday_bars(sym, sec)
            except Exception as ex:
                log.debug("분봉 채우기 실패 %s: %r", sym, ex)
                continue
            for b in bars[-s.view.maxlen:]:
                if b.ts < cur:
                    s.view.append((b.ts, b.open, b.high, b.low, b.close, b.volume))

    async def day_end(self, d: date) -> None:
        if self._day_ended == d:
            return
        self._day_ended = d
        self.phase = "POST"
        e = self.engine
        now = self.broker.now()
        e.flush_bars()
        await self.executor.drain(3.0)
        e.mark()
        dn = self.m.local_day(self.m.epoch(d, 12))
        if e.day == dn:
            td = list(e.trades_day)
            pnl = e.equity - e.day_start_equity
        else:
            # 그날 시세를 한 번도 받지 못했다(시세 장애, 휴장일 표에 없는 휴장). 엔진의 '오늘' 값은 아직 앞 거래일의
            # 것이므로 그대로 쓰면 전날의 손익·거래가 한 번 더 기록된다(복기도 같은 손실을 두 번 센다).
            td = [t for t in e.trades_day if self.m.local_day(t.exit_ts) == dn]
            pnl = sum(t.pnl for t in td)
            e.emit("system", "", f"{d} 에는 시세를 한 번도 받지 못했습니다 — 손익 없는 날로 기록합니다")
        wins = sum(1 for t in td if t.pnl > 0)
        day = str(d)
        self.journal.daily(day, e.equity, pnl, len(td), wins, e.p.version)
        await asyncio.to_thread(self.journal.flush)
        report = {"lessons": [], "adopted": None}
        if self.cfg.learn.enabled:
            bars = await asyncio.to_thread(self._recent_bars, now)
            self.learner.daily_hist = self.screener.bars     # 재생에서도 실매매처럼 일봉 맥락을 쓰게 한다
            recent = (self.history + e.trades)[-300:]
            blocks = {**self.engine.guard.blocks, **self.engine.risk.blocks}
            try:
                report = await self.learner.review(day, td, recent, bars, blocks, now)
            except Exception as ex:                    # 복기 실패가 다음 날 매매를 막지 않는다
                log.exception("복기 실패")
                e.emit("system", "", f"복기 실패: {ex!r}")
            e.set_params(self.store.current)
        self.days.append({"day": day, "equity": round(e.equity, 2), "pnl": round(pnl, 2), "n": len(td),
                          "wins": wins, "version": e.p.version,
                          "adopted": report["adopted"]["name"] if report.get("adopted") else ""})
        del self.days[:-120]
        self.phase = "CLOSED"
        self.note(f"{day} 마감 {self.m.money(pnl)} · {len(td)}건", now)
        e.emit("system", "", f"{day} 장 마감 — 손익 {self.m.money(pnl)}, 거래 {len(td)}건, 전략 v{e.p.version}")
        self.notifier.send(f"[{day} 마감] 손익 {self.m.money(pnl)} · 거래 {len(td)}건(승 {wins}) · 자산 "
                           f"{self.m.money(e.equity, signed=False)} · 전략 v{e.p.version}\n"
                           + "\n".join(report.get("lessons", [])[:4]))
        self._save_state()

    def _recent_bars(self, now: float) -> list:
        n = self.cfg.learn.lookback_days
        day = self.m.local_day
        if self.daily:
            # 일봉 방식: 증권사가 주는 과거 일봉 + 오늘 기록한 봉. 재생이 장 시간 안에서 돌도록 시각을 장 시작으로 맞춘다.
            from .models import Bar
            oh, om = divmod(self.cfg.session.tod("open") // 60, 60)
            seen, out = set(), []
            today = self.journal.load_bars(now - 86400)
            d0 = self.m.epoch(self.today) if self.today else now - 86400
            for src in (*self.screener.bars.values(), today):
                for b in src:
                    if src is not today and b.ts >= d0:
                        continue                      # 장중에 받아 둔 오늘 일봉은 미완성 → 엔진이 마감까지 기록한 봉을 쓴다
                    loc = self.m.local(b.ts)
                    key = (b.symbol, loc.date())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(Bar(b.symbol, self.m.epoch(loc.date(), oh, om), b.open, b.high, b.low, b.close, b.volume))
            out.sort(key=lambda b: (b.ts, b.symbol))
            days = sorted({day(b.ts) for b in out})[-n:]
            return [b for b in out if days and day(b.ts) >= days[0]]
        bars = self.journal.load_bars(now - n * 2 * 86400 - 86400)
        days = sorted({day(b.ts) for b in bars})[-n:]
        if not days:
            return []
        lo = days[0]
        return [b for b in bars if day(b.ts) >= lo]

    async def _scheduler(self) -> None:
        """실매매용 시계: 거래소 현지 시각 기준으로 장전/장후 일과를 호출한다."""
        ses = self.cfg.session
        pre, post = ses.tod("pre_open"), ses.tod("post")
        checked: dict[date, bool] = {}
        while True:
            n = self.m.now()
            today, tod = n.date(), n.hour * 3600 + n.minute * 60 + n.second
            if today not in checked:
                try:
                    checked[today] = await self.broker.is_trading_day(today)
                except Exception:
                    checked[today] = today.weekday() < 5
                if not checked[today]:
                    self.phase = "CLOSED"
                    self.note(f"{today} 휴장", time.time())
            if checked[today]:
                try:
                    if self._day_started != today and pre <= tod < post:
                        await self.day_start(today)
                    elif self._day_started == today and self._day_ended != today and tod >= post:
                        await self.day_end(today)
                except Exception as ex:                # 일시적 장애(네트워크 등) — 죽지 않고 잠시 뒤 다시 시도
                    log.exception("일과 진행 실패")
                    self.engine.emit("system", "", f"일과 진행 실패, 재시도 예정: {ex!r}")
                    if self.phase == "PRE":
                        self._day_started = None
                    await asyncio.sleep(25.0)
            await asyncio.sleep(5.0)

    async def _timer_loop(self) -> None:
        e = self.engine
        ses = self.cfg.session
        open_tod, close_tod = ses.tod("open"), ses.tod("close")
        while True:
            await asyncio.sleep(0.25)
            self.calls += 1
            mono = time.monotonic()
            n0, t0 = self._tick_mark
            if mono - t0 >= 1.0:
                self.tps = (e.ticks - n0) / (mono - t0)
                if e.ticks != n0:
                    self._last_tick_wall = mono
                self._tick_mark = (e.ticks, mono)
            realtime = self.live or self.manual
            self.executor.on_timer(self.executor.clock())
            if realtime and mono - self._clock_ts >= 60.0:
                self._clock_ts = mono
                self._check_clock()
            self._forward_alerts()
            if self.phase != "OPEN":
                self.broker.expect_data = False
                continue
            now = e.now
            if now - self._mkt_ts >= 60.0 and not self._mkt_busy:
                self._mkt_ts = now
                self._mkt_busy = True
                asyncio.create_task(self._market_update())
            if realtime:
                k = self.m.now()
                tod = k.hour * 3600 + k.minute * 60 + k.second
                self._watch_feed(mono, open_tod + 60 <= tod < close_tod)
            if self.live:
                if mono - self._recon_ts >= 60.0:
                    self._recon_ts = mono
                    try:
                        await self.reconcile()
                    except Exception as ex:
                        e.emit("system", "", f"계좌 대사 실패: {ex!r}")
            if self.keep_state and mono - self._save_ts >= 60.0:
                self._save_state()                      # 고점·추적 손절가·마지막 가격을 주기적으로 남긴다(체결이 없어도)

    def _forward_alerts(self) -> None:
        """엔진·집행기가 낸 위험 이벤트(손실 한도 발동, 확인 불가 주문, 거부 반복 정지, 심각한 악재 청산 등)를
        텔레그램으로도 알린다. 사람이 화면을 보고 있지 않아도 '멈췄다·팔았다'는 사실은 알아야 한다.
        알림을 직접 보내는 곳의 이벤트는 sent 표시가 있어 건너뛴다(같은 내용을 두 번 보내지 않는다)."""
        e = self.engine
        last = self._alert_seq
        if e._seq == last:
            return
        self._alert_seq = e._seq
        new = []
        for ev in reversed(e.events):
            if ev["seq"] <= last:
                break
            if (ev["kind"] == "risk" or ev.get("alert")) and not ev.get("sent"):
                new.append(ev)
        for ev in reversed(new[:5]):                    # 한 번에 최근 5건까지(폭주 방지)
            self.notifier.send("⚠ " + (f"[{ev['symbol']}] " if ev["symbol"] else "") + ev["text"])
        if len(new) > 5:
            self.notifier.send(f"⚠ 그 밖의 경고 {len(new) - 5}건은 대시보드 로그에서 확인하세요")

    def _check_clock(self) -> None:
        """이 컴퓨터의 시계가 서버와 크게 어긋나 있으면 알린다(장 시작·마감 판단, 시세 신선도, 주문 시각 비교가 모두 틀어진다)."""
        off = float(getattr(self.broker, "clock_off", 0.0) or 0.0)
        bad = abs(off) > 120.0
        if bad and not self._clock_bad:
            self.engine.emit("risk", "", f"이 컴퓨터의 시계가 서버보다 {abs(off):.0f}초 {'느립니다' if off > 0 else '빠릅니다'} — "
                             "장 시작·마감 판단과 시세·주문 시각 비교가 그만큼 어긋납니다. 운영체제의 시간 동기화를 켜세요")
        self._clock_bad = bad

    def _watch_feed(self, mono: float, in_session: bool) -> None:
        """시세가 들어와야 할 시간인데 끊겼는지 본다. 끊기면 신규 매수를 보류하고 알린다."""
        e = self.engine
        if in_session and not self.broker.expect_data:
            self._last_tick_wall = mono                 # 방금 장이 열렸다 — 지금부터 기다린다(장 전의 무소식은 끊김이 아니다)
        self.broker.expect_data = in_session
        stale = in_session and mono - self._last_tick_wall > self.stale_sec
        if stale != e.risk.feed_stale:
            e.risk.feed_stale = stale
            e.emit("risk", "", "시세 수신 지연 — 신규 매수 보류" if stale else "시세 수신 정상화", sent=1)
            self.notifier.send("⚠ 시세 수신이 끊겼습니다 — 신규 매수를 보류합니다" if stale else "시세 수신이 다시 정상입니다")

    async def _market_update(self) -> None:
        try:
            reg = await self.market.update(self.engine, self.broker, force=self._first_mkt)
            if self.market.metrics:
                self._first_mkt = False
            self.status = "warn" if reg.block_entries else "ok"
        except Exception:
            log.exception("시장분석 실패")
        finally:
            self._mkt_busy = False

    # ------------------------------------------------------------------ 체결·상태 저장
    def _on_fill(self, fl) -> None:
        self.journal.fill(fl)
        if self.keep_state:
            self._save_state()

    def _on_ticket(self, t: dict) -> None:
        """주문 보조 모드: 새 주문 제안을 로그와 알림으로 내보낸다."""
        side = "매수" if t["side"] == "BUY" else "매도"
        px = "시장가(급함)" if t["market"] else f"지정가 {self.m.px(t['price'])}"
        msg = f"[주문 제안] {t['symbol']} {side} {t['qty']}주 · {px} · 사유 {reason_ko(t['reason'])}"
        if t.get("stop"):
            msg += f" · 손절 예정가 {self.m.px(t['stop'])}"
        self.engine.emit("ticket", t["symbol"], msg)
        self.notifier.send(msg + f"\n{self.cfg.manual.ticket_ttl_min:.0f}분 안에 직접 주문한 뒤 대시보드에서 [체결됨]을 눌러 주세요.")

    def _on_trade(self, t: Trade) -> None:
        """거래가 끝나는 즉시 원인 태그를 붙여 일지에 남긴다(중간에 꺼져도 기록이 남게)."""
        t.causes = self.learner.tag(t, self.engine.p)
        self.journal.trade(t)

    def _save_state(self) -> None:
        """재시작 대비: 장부(현금·실현손익·당일 한도 상태·포지션)와 진행 중 주문을 디스크에 남긴다."""
        if not (self.keep_state and self.dir):
            return
        self._save_ts = time.monotonic()
        data = {"engine": self.engine.export_state(), "orders": self.executor.export(), "ts": time.time(),
                "broker": self.broker.export_state(), "acct_extra": self._acct_extra}
        try:
            tmp = self.dir / "state.tmp"
            # default=str: 주문 맥락(ctx)에 JSON 으로 못 쓰는 값이 섞여 있어도 저장은 되게 한다
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self.dir / "state.json")
        except (OSError, TypeError, ValueError) as ex:  # 디스크·직렬화 문제로 매매 루프(타이머)가 죽으면 안 된다
            if not self._save_failed:
                self._save_failed = True
                self.engine.emit("system", "", f"상태 저장 실패(매매는 계속): {ex!r}")
                self.notifier.send(f"⚠ 상태 저장에 실패했습니다({ex!r}) — 디스크를 확인하세요. 이 상태로 재시작하면 장부가 어긋날 수 있습니다.")
        else:
            self._save_failed = False

    async def reconcile(self, initial: bool = False) -> None:
        """증권사 계좌와 내부 장부를 맞춘다. 봇이 산 종목만 관리하고 그 외 보유분은 건드리지 않는다."""
        e, ex = self.engine, self.executor
        if initial:
            await self._restore(await self.broker.get_account())
            return
        if e.pending or ex.busy():
            return
        ver = (e.fill_seq, ex._n)                       # 조회 전 '장부 버전'
        acct = await self.broker.get_account()
        if (e.fill_seq, ex._n) != ver or e.pending or ex.busy():
            return                                      # 조회하는 사이 주문·체결이 있었다 → 낡은 스냅샷은 버린다
        hold = ex.unsettled()                           # 취소 뒤 지켜보는 중인 주문이 있는 종목 — 그 체결이 확정된 뒤에 맞춘다
        mism = {}
        for sym, pos in e.positions.items():
            bq = int(acct.positions.get(sym, (0, 0.0))[0])
            if bq < pos.qty and sym not in hold:
                mism[sym] = bq
        self._note_extra(acct, hold)
        confirmed = {s: q for s, q in mism.items() if self._recon_prev.get(s) == q}   # 두 번 연속 같은 불일치만 인정
        self._recon_prev = mism
        for sym, bq in confirmed.items():
            pos = e.positions[sym]
            missing = pos.qty - bq
            last = e.sym[sym].last or pos.avg_price
            msg = f"{sym} 장부 {pos.qty}주 ≠ 계좌 {bq}주 — {missing}주를 현재가에 판 것으로 장부를 정리하고 신규 매수를 멈춥니다"
            e.emit("risk", sym, "계좌 대사: " + msg, sent=1)
            # 그냥 지우면 자산이 그만큼 사라진 것처럼 보여 손실 한도가 잘못 발동한다 → 매도로 기록해 현금을 맞춘다
            e.on_fill(Fill(sym, SELL, missing, last, e.now or time.time(), reason="reconcile"))
            e.risk.halt(f"{sym} 장부·계좌 불일치 — 계좌 확인 후 [재개]", flatten=False)
            self.notifier.send("⛔ 계좌 대사 불일치: " + msg)
            self._recon_prev.pop(sym, None)
        if confirmed:
            self._save_state()

    def _note_extra(self, acct, hold: set) -> None:
        """계좌에 장부보다 많은 수량이 있으면 한 번 알린다(수량이 달라지면 다시). 봇은 자기가 산 것만 관리하므로
        멈추거나 팔지는 않는다 — 하지만 봇이 모르는 체결(잊힌 주문이 체결된 경우)이라면 그 주식에는 손절도 없으므로,
        조용히 넘어가지 않고 사람이 확인하게 한다. 직접 보유한 주식이면 무시하면 된다."""
        e = self.engine
        for sym in dict.fromkeys(list(e.positions) + list(self.symbols)):
            pos = e.positions.get(sym)
            extra = int(acct.positions.get(sym, (0, 0.0))[0]) - (pos.qty if pos is not None else 0)
            if extra <= 0 or sym in hold or sym in e.pending:
                if extra <= 0:
                    self._acct_extra.pop(sym, None)
                continue
            if self._acct_extra.get(sym) != extra:
                self._acct_extra[sym] = extra
                e.emit("risk", sym, f"{sym}: 계좌에 장부보다 {extra}주 더 있습니다 — 봇이 모르는 체결일 수 있습니다. "
                       "이 수량은 봇이 관리하지 않습니다(손절 없음). 직접 보유한 주식이면 무시하세요")

    async def _restore(self, acct) -> None:
        """시작 시: 저장된 장부가 있으면 이어받고, 없으면 계좌 현금(운용 한도 이내)으로 새로 시작한다.

        이어받는 순서가 중요하다(순서가 틀리면 멀쩡한 계좌를 '큰 손실'로 착각해 전량 청산이 나간다):
          1) 장부를 저장된 그대로 되살린다. 보유 종목은 취득가가 아니라 저장 시점의 '마지막 가격'으로 평가한다.
          2) 꺼져 있는 동안 체결된 주문을 그 장부에 반영한다.
          3) 그다음에 계좌와 비교한다. 계좌에 없는 수량은 그냥 지우지 않고 '판 것'으로 정리(현금 반영)한 뒤
             신규 매수를 멈추고 사람에게 알린다.
        acct=None 은 주문 보조 모드(계좌를 조회할 수 없다 → 저장된 장부를 그대로 믿는다)."""
        e = self.engine
        f = self.dir / "state.json" if self.dir else None
        st = None
        if f and f.exists():
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                e.emit("system", "", "저장된 상태 파일을 읽을 수 없어 새로 시작합니다")
        cap = self.cfg.capital
        if not st or "engine" not in st:
            cash = cap if acct is None else (min(acct.cash, cap) if cap > 0 else acct.cash)
            e.cash = cash
            e.equity = cash
            e.day_start_equity = cash
            e.risk.peak_equity = cash                   # 고점은 '설정 자본'이 아니라 실제 시작 자산에서 출발
            e.risk.start_equity = cash
            e.emit("system", "", f"계좌 연결 — 운용 현금 {self.m.money(cash, signed=False)}")
            return
        es = st["engine"]
        # 1) 저장된 장부 그대로
        lasts = es.get("last") or {}
        for pd in es.get("positions", []):
            pd = {k: v for k, v in pd.items() if k in _POS_FIELDS}
            if int(pd.get("qty", 0)) <= 0:
                continue
            pos = Position(**pd)
            e.positions[pos.symbol] = pos
            px = float(lasts.get(pos.symbol) or 0.0)
            e.symbol_state(pos.symbol).last = px if px > 0 else pos.avg_price
        e.cash = max(0.0, float(es.get("cash", cap)))
        e.realized_total = float(es.get("realized_total", 0.0))
        e.fees_total = float(es.get("fees_total", 0.0))
        for sym, since in (es.get("since_loss") or {}).items():
            st_sym = e.symbol_state(sym)                # 손실 청산 뒤 재진입 금지 기간을 이어 간다
            st_sym.last_loss_bar = st_sym.bar_count - int(since)
        e.exit_now.update(x for x in (es.get("exit_now") or []) if x in e.positions)
        left = es.get("exit_left") or {}
        for x in e.exit_now:                            # 지시가 살아 있을 남은 장 수(저장된 값이 없으면 처음부터)
            e.exit_left[x] = max(1, int(left.get(x, EXIT_SESSIONS)))
        # 오늘 이미 마친 일봉 판단(같은 날 다시 켜도 두 번 판단하지 않는다)
        e.decided.update({str(k): int(v) for k, v in (es.get("decided") or {}).items()})
        e.bar_exit.update({k: str(v) for k, v in (es.get("bar_exit") or {}).items() if k in e.positions})
        eq = e.cash + sum(p.qty * e.sym[p.symbol].last for p in e.positions.values())
        same_day = int(es.get("day", -1)) == self.m.local_day(time.time())
        if same_day:                                    # 같은 거래일에 다시 켜졌다 → 당일 손익·한도 상태를 이어간다
            e.day = int(es["day"])
            e.day_start_equity = float(es.get("day_start_equity", eq))
            e.realized_day = float(es.get("realized_day", 0.0))
            e.trades_day = [t for t in self.history if self.m.local_day(t.exit_ts) == e.day]
        else:
            e.day_start_equity = eq                     # 장전 준비 때 전일 종가 기준으로 다시 잡는다(_mark_at_prev_close)
        e.equity = eq
        e.risk.start_equity = e.day_start_equity
        e.risk.peak_equity = eq
        e.risk.restore(es.get("risk", {}), same_day)
        e.risk.peak_equity = max(e.risk.peak_equity, eq)
        # 2) 꺼져 있는 동안의 체결
        try:
            self.broker.import_state(st.get("broker") or {})      # 이 봇이 냈던 주문번호 등(예전 주문과 혼동하지 않게)
        except Exception as ex:
            e.emit("system", "", f"증권사 연동 상태를 이어받지 못했습니다: {ex!r}")
        saved_orders = st.get("orders", [])
        n = await self.executor.recover(saved_orders) if self.live else 0
        # 3) 계좌와 비교
        mism = []
        if acct is not None:
            if saved_orders:
                acct = await self.broker.get_account()  # 주문 정리(체결 반영·취소) 뒤의 계좌를 다시 본다
            # 이전 실행의 주문 가운데 체결 수량을 아직 확정하지 못한 것(확인 불가로 남긴 주문, 취소 뒤 더 지켜보는 주문)이
            # 걸린 종목은 여기서 맞추지 않는다. 계좌와의 차이가 바로 그 주문의 체결일 수 있어서, 지금 '판 것'으로 정리하면
            # 나중에 그 체결이 확인될 때 같은 수량을 한 번 더 반영하게 된다. 확정된 뒤 주기적 대사가 맞춘다.
            hold = self.executor.unsettled()
            for sym, pos in list(e.positions.items()):
                bq = int(acct.positions.get(sym, (0, 0.0))[0])
                if bq < pos.qty and sym in hold:
                    e.emit("risk", sym, f"{sym} 장부 {pos.qty}주 ≠ 계좌 {bq}주 — 이전 실행의 주문 체결이 확정될 때까지 "
                           "계좌 대사를 미룹니다")
                elif bq < pos.qty:
                    missing = pos.qty - bq
                    last = e.sym[sym].last or pos.avg_price
                    mism.append(f"{sym} 장부 {pos.qty}주 ≠ 계좌 {bq}주")
                    # 그냥 지우면 그만큼의 자산이 사라진 것처럼 보여 낙폭 한도가 잘못 발동한다 → 판 것으로 기록
                    e.on_fill(Fill(sym, SELL, missing, last, time.time(), reason="reconcile"))
            self._acct_extra = {str(k): int(v) for k, v in (st.get("acct_extra") or {}).items()}
            self._note_extra(acct, hold)
            if acct.cash < e.cash - max(1.0, 0.001 * e.cash):
                e.emit("risk", "", f"계좌의 주문가능금액({self.m.money(acct.cash, signed=False)})이 장부 현금"
                       f"({self.m.money(e.cash, signed=False)})보다 적습니다 — 매수 주문이 거부될 수 있습니다")
        e.mark()
        if mism:
            msg = "계좌 대사(재시작): " + ", ".join(mism) + " — 모자란 수량은 마지막 가격에 판 것으로 정리했습니다"
            e.emit("risk", "", msg, sent=1)
            e.risk.halt("장부·계좌 불일치 — 계좌 확인 후 [재개]", flatten=False)
            self.notifier.send("⛔ " + msg + ". 신규 매수를 멈췄습니다. 계좌를 확인한 뒤 [재개]를 눌러 주세요.")
        e.emit("system", "", f"계좌 연결 — 장부 복원: 현금 {self.m.money(e.cash, signed=False)}, 포지션 "
               f"{len(e.positions)}개, 이어받은 체결 {n}건" + (" · 당일 한도 상태 유지" if same_day else ""))
        self._save_state()

    # ------------------------------------------------------------------ 수동 제어(대시보드)
    def control(self, action: str) -> str:
        r = self.engine.risk
        e = self.engine
        if action == "halt":
            r.halt("수동 정지", flatten=False)
            msg = "수동 정지 — 신규 매수 중단(보유분은 계속 관리)"
        elif action == "flatten":
            r.halt("수동 전량청산", flatten=True)
            msg = "수동 전량청산 — 모든 보유분 매도 후 정지"
        elif action == "resume":
            freed = self.executor.drop_stuck()
            e.mark()
            r.resume(e.equity)                          # 낙폭·일일 손실 기준을 현재 자산으로 다시 잡는다
            self._recon_ts = 0.0                        # 곧바로 계좌 대사
            msg = "재개 — 정지 해제(손실 한도 기준을 현재 자산으로 재설정)" + (
                f" · 확인 불가 주문 해제: {', '.join(freed)}" if freed else "")
        else:
            return "알 수 없는 명령"
        e.emit("risk", "", msg, sent=1)
        self.notifier.send(msg)
        self._save_state()
        return msg

    def _on_late_fill(self, t: dict, qty: int, price: float) -> tuple[bool, str]:
        """주문 보조 모드: 만료·건너뛴 제안을 사람이 뒤늦게 체결했다고 알렸다 → 장부에 직접 반영한다."""
        e = self.engine
        sym, side = t["symbol"], t["side"]
        if sym in e.pending:
            return False, f"{sym} 에 새 주문 제안이 떠 있습니다 — 그 제안에서 처리하세요(장부에 반영되지 않았습니다)"
        pos = e.positions.get(sym)
        if side == SELL and (pos is None or qty > pos.qty):
            return False, f"{sym} 보유 수량보다 많이 팔 수는 없습니다 — 장부에 반영되지 않았습니다"
        s = e.symbol_state(sym)
        s.last = s.last or price
        e.on_fill(Fill(sym, side, qty, price, self.broker.now(), reason=t["reason"], ctx=dict(t.get("ctx") or {})))
        # 제안이 만료·건너뜀으로 끝났을 때 걸린 '다시 제안하지 않는 시간'을 푼다. 체결된 제안은 성공한 주문과 같다 —
        # 그대로 두면 그 시간(기본 30분) 동안 방금 산 종목의 손절·익절·악재 청산 제안이 하나도 뜨지 않는다.
        s.retry_after = 0.0
        e.emit("order", sym, f"만료된 제안의 늦은 체결 반영: {side} {qty}주 @ {self.m.px(price)}")
        return True, f"{sym} {side} {qty}주 @ {self.m.px(price)} 늦은 체결 반영"

    def manual_sell(self, symbol: str, qty: int | None, price: float) -> tuple[bool, str]:
        """주문 보조 모드: 사용자가 봇의 제안 없이 증권사 앱에서 직접 판 것(걸어 둔 손절 주문 체결 등)을 장부에 반영.
        (반영 여부, 메시지)"""
        if not self.manual:
            return False, "주문 보조 모드에서만 쓸 수 있습니다"
        e = self.engine
        pos = e.positions.get(symbol)
        if pos is None:
            return False, f"{symbol} 보유 기록이 없습니다"
        if symbol in e.pending:
            return False, f"{symbol} 에 대기 중인 주문 제안이 있습니다 — 그 제안에서 [체결됨] 또는 [건너뜀]을 먼저 누르세요"
        q = pos.qty if qty is None else int(qty)
        if q <= 0 or q > pos.qty or not price > 0:
            return False, "수량·가격을 확인하세요 — 장부에 반영되지 않았습니다"
        if hasattr(self.broker, "plausible") and not self.broker.plausible(symbol, float(price), e.sym[symbol].last):
            return False, "입력한 가격이 현재가와 너무 다릅니다 — 가격을 확인하세요(장부에 반영되지 않았습니다)"
        e.on_fill(Fill(symbol, SELL, q, float(price), self.broker.now(), reason="manual_sell"))
        return True, f"{symbol} {q}주 @ {self.m.px(price)} 직접 매도 반영"

    # ------------------------------------------------------------------ 대시보드 스냅샷
    def chart(self, symbol: str, since_ts: float = 0.0) -> dict | None:
        """선택한 종목의 실시간 차트 자료: 마감된 봉(지표 포함) + 진행 중인 봉 + 오늘 체결 표시."""
        e = self.engine
        s = e.sym.get(symbol)
        if s is None:
            return None
        bars = [list(b) for b in s.hist if b[0] > since_ts]
        cur = [s.bar_start, s.o, s.h, s.l, s.c, s.v] if s.bar_start >= 0 else None
        view = [list(b) for b in s.view if b[0] > since_ts]          # 화면용 분봉(전략 봉이 일봉일 때의 실시간 차트)
        vcur = [s.v_start, s.vo, s.vh, s.vl, s.vc, s.vv] if s.v_start >= 0 else None
        marks = []
        for t in e.trades_day:
            if t.symbol == symbol:
                marks.append({"ts": t.entry_ts, "price": t.entry_price, "side": "BUY"})
                marks.append({"ts": t.exit_ts, "price": t.exit_price, "side": "SELL", "pnl": round(t.pnl, 2),
                              "reason": t.exit_reason})
        pos = e.positions.get(symbol)
        levels = None
        if pos is not None:
            marks.append({"ts": pos.entry_ts, "price": pos.avg_price, "side": "BUY"})
            levels = {"avg": pos.avg_price, "stop": pos.stop, "target": pos.target}
        return {"symbol": symbol, "bar_sec": self.cfg.strategy.bar_sec, "bars": bars, "cur": cur,
                "view_sec": self.cfg.strategy.view_sec, "view": view, "vcur": vcur, "marks": marks, "levels": levels, "last": s.last, "cum_vol": s.cum_vol,
                "rvol": round(e.rvol_now(s), 2), "prev_close": s.prev_close}

    def snapshot(self, since_seq: int = 0) -> dict:
        e = self.engine
        r = e.risk
        reg = e.regime
        unreal = e.unrealized()
        pos = []
        for p in e.positions.values():
            last = e.sym[p.symbol].last
            pos.append({"symbol": p.symbol, "qty": p.qty, "avg": p.avg_price, "last": last,
                        "pnl": round(p.qty * last - p.cost, 2), "pnl_pct": round((last / p.avg_price - 1) * 100, 2),
                        "stop": p.stop, "target": p.target, "stop_kind": p.stop_kind, "bars": p.bars_held,
                        "entry_ts": p.entry_ts, "exit_flag": p.symbol in e.exit_now})
        watch = []
        for sym in self.symbols or list(e.sym):
            s = e.sym.get(sym)
            if s is None:
                continue
            f = s.f
            ref = s.prev_close or s.day_open
            ns = e.news.get(sym)
            watch.append({"symbol": sym, "last": s.last,
                          "chg": round((s.last / ref - 1) * 100, 2) if ref and s.last else 0.0,
                          "rsi": round(f.rsi), "score": round(s.score, 2), "trend": int(f.ema_fast > f.ema_slow),
                          "ready": f.ready, "held": sym in e.positions, "vol": s.cum_vol,
                          "rvol": round(e.rvol_now(s), 2), "news": round(ns.score, 2) if ns else 0.0,
                          "blocked": bool(ns and e.now < ns.block_until)})
        rep = self.learner.reports[-1] if self.learner.reports else None
        ses = self.cfg.session
        tickets = self.broker.tickets_view() if hasattr(self.broker, "tickets_view") else []
        return {
            "mode": self.cfg.mode, "broker": self.broker.name, "phase": self.phase, "now": e.now,
            "style": self.cfg.style, "bar_sec": self.cfg.strategy.bar_sec, "auto_apply": self.cfg.learn.auto_apply,
            "wall": time.time(), "day": str(self.today or ""), "uptime": round(time.time() - self.started),
            "market": {"code": self.m.code, "currency": self.m.currency, "decimals": self.m.decimals,
                       "tz": self.m.tz, "tz_off": e.tz_off if e.now else self.m.utc_offset(time.time()),
                       "open": ses.tod("open"), "close": ses.tod("close")},
            "halted": r.halted, "halt_reason": r.halt_reason, "day_stop": r.day_stop,
            "feed_stale": r.feed_stale, "flattening": r.flatten_all,
            "equity": round(e.equity, 2), "cash": round(e.cash, 2), "exposure": round(e.exposure, 2),
            "day_start_equity": round(e.day_start_equity, 2), "day_pnl": round(e.equity - e.day_start_equity, 2),
            "realized_day": round(e.realized_day, 2), "unrealized": round(unreal, 2),
            "drawdown": round(r.drawdown, 4), "fees_total": round(e.fees_total, 2),
            "limits": {"max_daily_loss": self.cfg.risk.max_daily_loss, "max_drawdown": self.cfg.risk.max_drawdown,
                       "max_exposure": self.cfg.risk.max_exposure, "max_positions": self.cfg.risk.max_positions},
            "regime": {"bias": reg.bias, "risk_mult": reg.risk_mult, "block": reg.block_entries,
                       "note": reg.note, "source": reg.source, "avoid": sorted(reg.avoid)},
            "market_metrics": self.market.metrics,
            "params": asdict(e.p),
            "positions": pos, "orders": self.executor.working(), "tickets": tickets, "watch": watch,
            "agents": [a.info() for a in self.agents],
            "latency": {"decision_us": e.lat.summary(1000.0), "order_ms": self.executor.rtt.summary(1000.0),
                        "ticks": e.ticks, "tps": round(self.tps, 1)},
            "blocks": {"guard": dict(e.guard.blocks), "risk": dict(r.blocks)},
            "today": summarize(e.trades_day), "total": summarize(e.trades),
            "llm": self.llm.info(),
            "news": self.news.recent(40) if self.news is not None else [],
            "news_on": self.news is not None,
            "learn": {"report": rep, "history": [{"version": h["version"], "reason": h["reason"]}
                                                 for h in self.store.history[-8:]]},
            "days": self.days[-60:],
            "equity_series": e.equity_series[-400:],
            "events": [ev for ev in e.events if ev["seq"] > since_seq],
            "seq": e._seq,
        }
