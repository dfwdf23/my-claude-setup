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
from .models import SELL, Fill, Position, Trade
from .notify import Notifier
from .params import ParamStore

log = logging.getLogger("aitrader")
_POS_FIELDS = {f.name for f in fields(Position)}


def store_dir(cfg: Config) -> Path:
    return Path(cfg.data_dir) / (cfg.mode if cfg.market == "KRX" else f"{cfg.mode}-{cfg.market.lower()}")


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
        self.store = ParamStore(d / "params.json" if d else None)
        self.llm = LLM(cfg.llm, self.journal)
        self.engine = Engine(cfg, self.store.current, cfg.capital)
        timeout = cfg.manual.ticket_ttl_min * 60.0 if self.manual else cfg.risk.order_timeout_sec
        self.executor = Executor(broker, self.engine, timeout)
        self.market = MarketAnalyst(cfg, self.llm)
        self.screener = Screener()
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
        if self.live:
            self.executor.on_change = self._save_state
        broker.on_tick = e.on_tick                     # 큐 없이 직결: 수신 루프 → 엔진
        broker.on_day_start = self.day_start
        broker.on_day_end = self.day_end
        broker.on_status = lambda msg: e.emit("system", "", msg)
        self.phase = "INIT"
        self.today: date | None = None
        self.symbols: list[str] = []
        self.days: list[dict] = []
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
        self._save_ts = 0.0
        self._day_started: date | None = None
        self._day_ended: date | None = None

    # ------------------------------------------------------------------ 실행
    async def run(self) -> None:
        await self.broker.start()
        if self.broker.has_account:
            await self.reconcile(initial=True)
        tasks = [asyncio.create_task(self.broker.run(), name="broker"),
                 asyncio.create_task(self._timer_loop(), name="timer"),
                 asyncio.create_task(self._stop.wait(), name="stop")]
        if not self.broker.drives_clock:
            tasks.append(asyncio.create_task(self._scheduler(), name="scheduler"))
        if self.news is not None:
            tasks.append(asyncio.create_task(self.news.run(), name="news"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.get_name() != "stop" and t.exception() is not None:
                    log.error("작업 %s 오류로 종료: %r", t.get_name(), t.exception())
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
            await self.broker.stop()
            self._save_state()
            await self.notifier.close()
            self.journal.close()

    def stop(self) -> None:
        self._stop.set()

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
        day0 = self.m.epoch(d)
        for sym in syms:
            s = e.symbol_state(sym)
            e.set_daily(sym, [b for b in self.screener.bars.get(sym, []) if b.ts < day0])   # 일봉 맥락(어제까지)
            if s.ind.n == 0:                          # 지표 예열: 어제까지 기록된 봉으로 미리 채운다
                for b in self.journal.recent_bars(sym, 3 * self.cfg.strategy.warmup_bars):
                    f = s.ind.update(b)
                    s.hist.append((b.ts, b.open, b.high, b.low, b.close, b.volume, f.ema_fast, f.ema_slow, f.vwap))
                s.ind.reset_session()
        if self.news is not None:
            self.news.new_day()
            self.news.set_symbols(syms)
        await self.broker.subscribe(syms)
        e.set_params(self.store.current)
        self.phase = "OPEN"
        self._last_tick_wall = time.monotonic()
        self.note(f"{d} 장 시작 — {len(syms)}종목 감시 · 전략 v{e.p.version}", now)
        e.emit("system", "", f"{d} 장 시작 — 감시 {len(syms)}종목, 전략 v{e.p.version}")

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
        td = list(e.trades_day)
        pnl = e.equity - e.day_start_equity
        wins = sum(1 for t in td if t.pnl > 0)
        day = str(d)
        self.journal.daily(day, e.equity, pnl, len(td), wins, e.p.version)
        await asyncio.to_thread(self.journal.flush)
        report = {"lessons": [], "adopted": None}
        if self.cfg.learn.enabled:
            bars = await asyncio.to_thread(self._recent_bars, now)
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
        bars = self.journal.load_bars(now - n * 2 * 86400 - 86400)
        day = self.m.local_day
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
            self.executor.on_timer(max(e.now, time.time()) if realtime else self.broker.now())
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
                in_session = open_tod + 60 <= tod < close_tod
                self.broker.expect_data = in_session
                stale = in_session and mono - self._last_tick_wall > self.stale_sec
                if stale != e.risk.feed_stale:
                    e.risk.feed_stale = stale
                    e.emit("risk", "", "시세 수신 지연 — 신규 매수 보류" if stale else "시세 수신 정상화")
                    if stale:
                        self.notifier.send("⚠ 시세 수신이 끊겼습니다 — 신규 매수를 보류합니다")
            if self.live:
                if mono - self._recon_ts >= 60.0:
                    self._recon_ts = mono
                    try:
                        await self.reconcile()
                    except Exception as ex:
                        e.emit("system", "", f"계좌 대사 실패: {ex!r}")
                if mono - self._save_ts >= 60.0:        # 고점·한도 상태를 주기적으로 남긴다
                    self._save_state()

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
        if self.live:
            self._save_state()

    def _on_trade(self, t: Trade) -> None:
        """거래가 끝나는 즉시 원인 태그를 붙여 일지에 남긴다(중간에 꺼져도 기록이 남게)."""
        t.causes = self.learner.tag(t, self.engine.p)
        self.journal.trade(t)

    def _save_state(self) -> None:
        """재시작 대비: 장부(현금·실현손익·당일 한도 상태·포지션)와 진행 중 주문을 디스크에 남긴다."""
        if not (self.live and self.dir):
            return
        self._save_ts = time.monotonic()
        data = {"engine": self.engine.export_state(), "orders": self.executor.export(), "ts": time.time()}
        tmp = self.dir / "state.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "state.json")

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
        mism = {}
        for sym, pos in e.positions.items():
            bq = int(acct.positions.get(sym, (0, 0.0))[0])
            if bq < pos.qty:
                mism[sym] = bq
        confirmed = {s: q for s, q in mism.items() if self._recon_prev.get(s) == q}   # 두 번 연속 같은 불일치만 인정
        self._recon_prev = mism
        for sym, bq in confirmed.items():
            pos = e.positions[sym]
            missing = pos.qty - bq
            last = e.sym[sym].last or pos.avg_price
            msg = f"{sym} 장부 {pos.qty}주 ≠ 계좌 {bq}주 — {missing}주를 현재가에 판 것으로 장부를 정리하고 신규 매수를 멈춥니다"
            e.emit("risk", sym, "계좌 대사: " + msg)
            # 그냥 지우면 자산이 그만큼 사라진 것처럼 보여 손실 한도가 잘못 발동한다 → 매도로 기록해 현금을 맞춘다
            e.on_fill(Fill(sym, SELL, missing, last, e.now or time.time(), reason="reconcile"))
            e.risk.halt(f"{sym} 장부·계좌 불일치 — 계좌 확인 후 [재개]", flatten=False)
            self.notifier.send("⛔ 계좌 대사 불일치: " + msg)
            self._recon_prev.pop(sym, None)
        if confirmed:
            self._save_state()

    async def _restore(self, acct) -> None:
        """시작 시: 저장된 장부가 있으면 이어받고, 없으면 계좌 현금(운용 한도 이내)으로 새로 시작한다."""
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
            e.cash = min(acct.cash, cap) if cap > 0 else acct.cash
            e.equity = e.cash
            e.day_start_equity = e.cash
            e.risk.peak_equity = e.cash                 # 고점은 '설정 자본'이 아니라 실제 시작 자산에서 출발
            e.risk.start_equity = e.cash
            e.emit("system", "", f"계좌 연결 — 운용 현금 {self.m.money(e.cash, signed=False)}")
            return
        es = st["engine"]
        for pd in es.get("positions", []):
            bq = int(acct.positions.get(pd["symbol"], (0, 0.0))[0])
            q = min(int(pd["qty"]), bq)
            if q <= 0:
                e.emit("risk", pd["symbol"], f"이전 실행의 {pd['symbol']} 포지션이 계좌에 없어 제외합니다")
                continue
            pd = {k: v for k, v in pd.items() if k in _POS_FIELDS}
            pd["cost"] = pd["cost"] * q / pd["qty"]
            pd["qty"] = q
            e.positions[pd["symbol"]] = Position(**pd)
            e.symbol_state(pd["symbol"]).last = pd["avg_price"]
        e.cash = max(0.0, min(float(es.get("cash", cap)), acct.cash))
        e.realized_total = float(es.get("realized_total", 0.0))
        e.fees_total = float(es.get("fees_total", 0.0))
        eq = e.cash + sum(p.qty * e.sym[p.symbol].last for p in e.positions.values())
        same_day = int(es.get("day", -1)) == self.m.local_day(time.time())
        if same_day:                                    # 같은 거래일에 다시 켜졌다 → 당일 손익·한도 상태를 이어간다
            e.day = int(es["day"])
            e.day_start_equity = float(es.get("day_start_equity", eq))
            e.realized_day = float(es.get("realized_day", 0.0))
            e.trades_day = [t for t in self.history if self.m.local_day(t.exit_ts) == e.day]
        else:
            e.day_start_equity = eq
        e.risk.start_equity = e.day_start_equity
        e.risk.peak_equity = eq
        e.risk.restore(es.get("risk", {}), same_day)
        e.risk.peak_equity = max(e.risk.peak_equity, eq)
        e.mark()
        n = await self.executor.recover(st.get("orders", []))
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
        e.emit("risk", "", msg)
        self.notifier.send(msg)
        self._save_state()
        return msg

    # ------------------------------------------------------------------ 대시보드 스냅샷
    def chart(self, symbol: str, since_ts: float = 0.0) -> dict | None:
        """선택한 종목의 실시간 차트 자료: 마감된 봉(지표 포함) + 진행 중인 봉 + 오늘 체결 표시."""
        e = self.engine
        s = e.sym.get(symbol)
        if s is None:
            return None
        bars = [list(b) for b in s.hist if b[0] > since_ts]
        cur = [s.bar_start, s.o, s.h, s.l, s.c, s.v] if s.bar_start >= 0 else None
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
                "marks": marks, "levels": levels, "last": s.last, "cum_vol": s.cum_vol,
                "rvol": round(s.rvol, 2), "prev_close": s.prev_close}

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
                        "entry_ts": p.entry_ts})
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
                          "rvol": round(s.rvol, 2), "news": round(ns.score, 2) if ns else 0.0,
                          "blocked": bool(ns and e.now < ns.block_until)})
        rep = self.learner.reports[-1] if self.learner.reports else None
        ses = self.cfg.session
        tickets = self.broker.tickets_view() if hasattr(self.broker, "tickets_view") else []
        return {
            "mode": self.cfg.mode, "broker": self.broker.name, "phase": self.phase, "now": e.now,
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
