"""총괄 에이전트 — 모든 에이전트를 묶고, 사람이 없어도 하루 일과가 저절로 돌게 한다.

  장전(08:40)  종목선정 → 지표 예열 → 시세 구독
  장중         틱마다 엔진(빠른 경로) · 매분 시장분석 · 주문 시간초과 정리 · 시세 끊김 감시 · 계좌 대사
  장후(15:40)  일지 기록 → 복기(손실 원인 분석·검증·파라미터 갱신) → 다음 거래일 대기
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .agents.base import Agent
from .agents.learner import Learner
from .agents.market import MarketAnalyst, Screener
from .brokers.base import Broker
from .config import Config
from .engine import Engine
from .executor import Executor
from .journal import Journal
from .llm import LLM
from .metrics import summarize
from .models import Position, Trade
from .params import ParamStore

log = logging.getLogger("aitrader")
KST_TZ = timezone(timedelta(hours=9))


class Supervisor(Agent):
    name = "supervisor"
    role = "총괄"
    kind = "slow"

    def __init__(self, cfg: Config, broker: Broker, persist: bool = True) -> None:
        super().__init__()
        self.cfg = cfg
        self.broker = broker
        self.live = cfg.mode != "sim"
        d = Path(cfg.data_dir) / cfg.mode if persist else None
        self.dir = d
        self.journal = Journal(d / "journal.db" if d else None)
        self.store = ParamStore(d / "params.json" if d else None)
        self.llm = LLM(cfg.llm, self.journal)
        self.engine = Engine(cfg, self.store.current, cfg.capital)
        self.executor = Executor(broker, self.engine, cfg.risk.order_timeout_sec)
        self.market = MarketAnalyst(cfg, self.llm)
        self.screener = Screener()
        self.learner = Learner(cfg, self.store, self.llm, self.journal)
        self.agents: list[Agent] = [self, self.market, self.screener, *self.engine.agents,
                                    self.executor, self.learner]
        e = self.engine
        e.on_bar_cb = self.journal.bar
        e.on_fill_cb = self._on_fill
        e.on_trade_cb = self._on_trade
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
        self._stop = asyncio.Event()
        self._mkt_ts = 0.0
        self._mkt_busy = False
        self._first_mkt = True
        self._tick_mark = (0, time.monotonic())
        self._last_tick_wall = time.monotonic()
        self.tps = 0.0
        self._recon_ts = 0.0
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
            await self.broker.stop()
            self._save_state()
            self.journal.close()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 하루 일과
    async def day_start(self, d: date) -> None:
        self.phase = "PRE"
        self.today = d
        self._day_started = d
        now = self.broker.now()
        self.llm.new_day(datetime.now(KST_TZ).strftime("%Y-%m-%d"))   # 토큰 예산은 실제 날짜 기준
        self.market.new_day()
        self._first_mkt = True
        syms = await self.screener.select(self.broker, self.cfg.universe, self.cfg.max_symbols, now)
        e = self.engine
        syms = list(dict.fromkeys(syms + list(e.positions)))   # 보유 중인 종목은 반드시 시세를 받아야 한다
        self.symbols = syms
        for sym in syms:                              # 지표 예열: 어제까지 기록된 봉으로 미리 채운다
            s = e.symbol_state(sym)
            if s.ind.n == 0:
                for b in self.journal.recent_bars(sym, 3 * self.cfg.strategy.warmup_bars):
                    s.ind.update(b)
                s.ind.reset_session()
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
        for t in td:
            self.journal.trade(t)
        self.days.append({"day": day, "equity": round(e.equity), "pnl": round(pnl), "n": len(td),
                          "wins": wins, "version": e.p.version,
                          "adopted": report["adopted"]["name"] if report.get("adopted") else ""})
        del self.days[:-120]
        self.phase = "CLOSED"
        self.note(f"{day} 마감 {pnl:+,.0f}원 · {len(td)}건", now)
        e.emit("system", "", f"{day} 장 마감 — 손익 {pnl:+,.0f}원, 거래 {len(td)}건, 전략 v{e.p.version}")
        self._save_state()

    def _recent_bars(self, now: float) -> list:
        n = self.cfg.learn.lookback_days
        bars = self.journal.load_bars(now - n * 2 * 86400 - 86400)
        days = sorted({int((b.ts + 32400) // 86400) for b in bars})[-n:]
        if not days:
            return []
        lo = days[0]
        return [b for b in bars if int((b.ts + 32400) // 86400) >= lo]

    async def _scheduler(self) -> None:
        """실매매용 시계: 한국시간 기준으로 장전/장후 일과를 호출한다."""
        ses = self.cfg.session
        pre, post = ses.tod("pre_open"), ses.tod("post")
        checked: dict[date, bool] = {}
        while True:
            n = datetime.now(KST_TZ)
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
            self.executor.on_timer(self.broker.now() if not self.live else max(e.now, time.time()))
            if self.phase != "OPEN":
                continue
            now = e.now
            if now - self._mkt_ts >= 60.0 and not self._mkt_busy:
                self._mkt_ts = now
                self._mkt_busy = True
                asyncio.create_task(self._market_update())
            if self.live:
                k = datetime.now(KST_TZ)
                tod = k.hour * 3600 + k.minute * 60 + k.second
                in_session = self.cfg.session.tod("open") + 60 <= tod < self.cfg.session.tod("close")
                stale = in_session and mono - self._last_tick_wall > self.cfg.risk.stale_feed_sec
                if stale != e.risk.feed_stale:
                    e.risk.feed_stale = stale
                    e.emit("risk", "", "시세 수신 지연 — 신규 매수 보류" if stale else "시세 수신 정상화")
                if mono - self._recon_ts >= 60.0:
                    self._recon_ts = mono
                    try:
                        await self.reconcile()
                    except Exception as ex:
                        e.emit("system", "", f"계좌 대사 실패: {ex!r}")

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
        pass                                          # 거래 기록은 복기에서 원인 태그를 붙인 뒤 저장한다

    def _save_state(self) -> None:
        """재시작 대비: 봇이 보유한 포지션과 정지 상태를 디스크에 남긴다(실매매 모드)."""
        if not (self.live and self.dir):
            return
        e = self.engine
        data = {"positions": [asdict(p) for p in e.positions.values()], "peak_equity": e.risk.peak_equity,
                "halted": e.risk.halted, "halt_reason": e.risk.halt_reason, "ts": time.time()}
        tmp = self.dir / "state.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "state.json")

    async def reconcile(self, initial: bool = False) -> None:
        """증권사 계좌와 내부 장부를 맞춘다. 봇이 산 종목만 관리하고 그 외 보유분은 건드리지 않는다."""
        acct = await self.broker.get_account()
        e = self.engine
        if initial:
            held_cost = 0.0
            f = self.dir / "state.json" if self.dir else None
            if f and f.exists():
                st = json.loads(f.read_text(encoding="utf-8"))
                for pd in st.get("positions", []):
                    bq = acct.positions.get(pd["symbol"], (0, 0.0))[0]
                    q = min(int(pd["qty"]), int(bq))
                    if q <= 0:
                        continue
                    pd["cost"] = pd["cost"] * q / pd["qty"]
                    pd["qty"] = q
                    e.positions[pd["symbol"]] = Position(**pd)
                    e.symbol_state(pd["symbol"]).last = pd["avg_price"]
                    held_cost += pd["cost"]
                e.risk.peak_equity = float(st.get("peak_equity", 0.0))
                if st.get("halted"):                   # 낙폭 정지는 재시작해도 풀리지 않는다
                    e.risk.halt(st.get("halt_reason") or "이전 실행에서 정지됨", flatten=False)
            cap = self.cfg.capital
            e.cash = max(0.0, min(acct.cash, cap - held_cost)) if cap > 0 else acct.cash
            e.mark()
            e.day_start_equity = e.equity
            e.risk.start_equity = e.equity
            e.risk.peak_equity = max(e.risk.peak_equity, e.equity)
            e.emit("system", "", f"계좌 연결 — 운용 현금 {e.cash:,.0f}원, 복원 포지션 {len(e.positions)}개")
            return
        if e.pending or self.executor.orders:
            return
        for sym, pos in list(e.positions.items()):
            bq = int(acct.positions.get(sym, (0, 0.0))[0])
            if bq < pos.qty:
                e.emit("risk", sym, f"계좌 대사: 장부 {pos.qty}주 ≠ 계좌 {bq}주 — 계좌 기준으로 정정")
                if bq <= 0:
                    del e.positions[sym]
                else:
                    pos.cost *= bq / pos.qty
                    pos.qty = bq
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
            r.resume()
            msg = "재개 — 정지 해제"
        else:
            return "알 수 없는 명령"
        e.emit("risk", "", msg)
        self._save_state()
        return msg

    # ------------------------------------------------------------------ 대시보드 스냅샷
    def snapshot(self, since_seq: int = 0) -> dict:
        e = self.engine
        r = e.risk
        reg = e.regime
        unreal = e.unrealized()
        pos = []
        for p in e.positions.values():
            last = e.sym[p.symbol].last
            pos.append({"symbol": p.symbol, "qty": p.qty, "avg": p.avg_price, "last": last,
                        "pnl": round(p.qty * last - p.cost), "pnl_pct": round((last / p.avg_price - 1) * 100, 2),
                        "stop": p.stop, "target": p.target, "stop_kind": p.stop_kind, "bars": p.bars_held,
                        "entry_ts": p.entry_ts})
        watch = []
        for sym in self.symbols or list(e.sym):
            s = e.sym.get(sym)
            if s is None:
                continue
            f = s.f
            watch.append({"symbol": sym, "last": s.last,
                          "chg": round((s.last / s.day_open - 1) * 100, 2) if s.day_open else 0.0,
                          "rsi": round(f.rsi), "score": round(s.score, 2), "trend": int(f.ema_fast > f.ema_slow),
                          "ready": f.ready, "held": sym in e.positions})
        rep = self.learner.reports[-1] if self.learner.reports else None
        return {
            "mode": self.cfg.mode, "broker": self.broker.name, "phase": self.phase, "now": e.now,
            "wall": time.time(), "day": str(self.today or ""), "uptime": round(time.time() - self.started),
            "halted": r.halted, "halt_reason": r.halt_reason, "day_stop": r.day_stop,
            "feed_stale": r.feed_stale, "flattening": r.flatten_all,
            "equity": round(e.equity), "cash": round(e.cash), "exposure": round(e.exposure),
            "day_start_equity": round(e.day_start_equity), "day_pnl": round(e.equity - e.day_start_equity),
            "realized_day": round(e.realized_day), "unrealized": round(unreal),
            "drawdown": round(r.drawdown, 4), "fees_total": round(e.fees_total),
            "limits": {"max_daily_loss": self.cfg.risk.max_daily_loss, "max_drawdown": self.cfg.risk.max_drawdown,
                       "max_exposure": self.cfg.risk.max_exposure, "max_positions": self.cfg.risk.max_positions},
            "regime": {"bias": reg.bias, "risk_mult": reg.risk_mult, "block": reg.block_entries,
                       "note": reg.note, "source": reg.source, "avoid": sorted(reg.avoid)},
            "market": self.market.metrics,
            "params": asdict(e.p),
            "positions": pos, "orders": self.executor.working(), "watch": watch,
            "agents": [a.info() for a in self.agents],
            "latency": {"decision_us": e.lat.summary(1000.0), "order_ms": self.executor.rtt.summary(1000.0),
                        "ticks": e.ticks, "tps": round(self.tps, 1)},
            "blocks": {"guard": dict(e.guard.blocks), "risk": dict(r.blocks)},
            "today": summarize(e.trades_day), "total": summarize(e.trades),
            "llm": self.llm.info(),
            "learn": {"report": rep, "history": [{"version": h["version"], "reason": h["reason"]}
                                                 for h in self.store.history[-8:]]},
            "days": self.days[-60:],
            "equity_series": e.equity_series[-400:],
            "events": [ev for ev in e.events if ev["seq"] > since_seq],
            "seq": e._seq,
        }
