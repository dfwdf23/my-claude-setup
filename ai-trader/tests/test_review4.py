"""네 번째 독립 검토(재시작·일과·봉 재생·소식)에서 재현된 결함의 회귀 테스트.

각 테스트는 고치기 전에 실제로 틀린 결과가 나오던 상황을 그대로 재현한다.
"""
import asyncio
import sqlite3
from datetime import date, timedelta

from aitrader.backtest import Backtester
from aitrader.brokers.base import Broker
from aitrader.config import Config
from aitrader.engine import Engine
from aitrader.journal import Journal
from aitrader.market import US
from aitrader.models import SELL, Bar, Fill, Tick
from aitrader.params import StrategyParams
from aitrader.supervisor import Supervisor

from test_review2 import (NOW, SYMS, SymLLM, _closes, feed, fresh, live_cfg, news_env2, open_book, prev_weekday,
                          restart, sells)


# ====================================================================== 재시작·장전 준비
def _series(d0: date, prev_close: float, base: float, partial: bool) -> dict:
    """d0 직전 30거래일의 일봉(어제 종가 prev_close, 그 전은 base). partial 이면 오늘의 진행 중인 봉을 끝에 붙인다."""
    hist, x = [], d0
    for i in range(30):
        x = prev_weekday(x)
        c = prev_close if i == 0 else base
        hist.append(Bar("", US.epoch(x, 9, 30), c, c + 0.5, c - 0.5, c, 1_000_000))
    hist.reverse()
    if partial:
        hist.append(Bar("", US.epoch(d0, 9, 30), prev_close, prev_close + 0.3, prev_close - 0.3, prev_close, 400_000))
    return {s: [Bar(s, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in hist] for s in SYMS}


async def test_midsession_restart_after_days_off_takes_yesterdays_close_as_todays_baseline(tmp_path):
    """며칠 꺼져 있다가 '장중에' 켜도 오늘 손익의 기준은 전일 종가다.
    (이전: 오늘의 진행 중인 일봉을 채우는 과정에서 엔진의 날짜가 먼저 오늘로 넘어가 전일 종가 평가를 건너뜀 →
           꺼져 있던 이틀의 −2.6% 가 '오늘 손실'이 되어 가격이 그대로인데도 9종목 전량 시장가 매도.
           반대로 그 사이 올랐으면 오늘의 실제 −2.4% 가 가려져 일일 손실 한도가 작동하지 않음)"""
    for name, close, today_px, stop in (("drop", 97.4, 97.4, False), ("rise", 103.0, 100.3, True)):
        cfg = live_cfg(tmp_path / name)
        d0 = US.now().date()
        d2 = prev_weekday(prev_weekday(d0))
        sup, br = await fresh(cfg)
        await open_book(sup, br, d2)
        feed(sup.engine, d2, 15, 59, 100.0)
        sup._save_state()                                    # 그제 저녁에 꺼졌다(마지막 가격 100)
        sup.journal.close()
        br.daily = _series(d0, close, 100.0, partial=True)   # 증권사 일봉: 어제 종가 + 오늘의 진행 중인 봉

        sup2, br2 = await restart(cfg, br)
        e2 = sup2.engine
        br2.now = lambda: US.epoch(d0, 11, 0)                # 오늘 11시에 켰다
        await sup2.day_start(d0)
        eq = e2.cash + 9 * 30 * close
        assert abs(e2.day_start_equity - eq) < 0.01 and abs(e2.risk.start_equity - eq) < 0.01, name
        assert e2.day == US.local_day(US.epoch(d0, 11, 0))   # 오늘 봉을 채우면서 엔진의 날짜는 오늘이 된다
        for k in (1, 2, 3):                                  # 한도는 평가 시점에 걸리고, 매도는 그다음 시세에 나간다
            feed(e2, d0, 11, k, today_px)
        await asyncio.sleep(0.03)
        assert e2.risk.day_stop == stop and e2.risk.flatten_all == stop, name
        assert len(sells(br2)) == (9 if stop else 0), name
        sup2.journal.close()


async def test_bar_exit_that_was_decided_but_not_filled_survives_a_restart(tmp_path):
    """결정됐지만 체결되지 않은 봉 기준 청산(추세 이탈·시간 청산)은 재시작해도 이어서 낸다.
    (이전: 저장하지 않아 재시작하면 잊음 → 일봉 방식은 판단이 하루 한 번이라 청산이 통째로 사라짐)"""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    sup, br = await fresh(cfg)
    e, ex = sup.engine, sup.executor
    await open_book(sup, br, d0)
    sym = SYMS[0]
    pos, s = e.positions[sym], e.sym[sym]
    pos.bars_held = 10_000                                   # 보유 기간 초과 → 시간 청산
    e._decide(s, s.f, 10 * 3600 + 60.0)
    await asyncio.sleep(0.05)
    assert e.bar_exit == {sym: "time_stop"} and e.pending[sym].side == SELL
    ex.on_timer(ex.clock() + cfg.risk.order_timeout_sec + 5.0)          # 지정가 매도가 체결되지 않고 시간 초과
    await asyncio.sleep(0.3)
    assert sym not in e.pending and sym in e.positions and e.bar_exit == {sym: "time_stop"}
    n0 = br.n
    sup._save_state()
    sup.journal.close()

    sup2, br2 = await restart(cfg, br)
    e2 = sup2.engine
    assert e2.bar_exit == {sym: "time_stop"}
    feed(e2, d0, 10, 5, 100.0)
    await asyncio.sleep(0.03)
    new = [(o["symbol"], o["side"]) for bid, o in br2.orders.items() if int(bid) > n0]
    assert new == [(sym, SELL)]                              # 그 종목의 매도만 다시 나간다
    sup2.journal.close()


async def test_a_day_without_any_tick_is_recorded_as_an_empty_day(tmp_path):
    """시세가 한 건도 오지 않은 거래일은 '손익 없는 날'로 기록한다.
    (이전: 엔진의 '오늘' 값이 전날 것인 채로 마감 → 전날의 손익·거래가 한 번 더 기록되고 복기도 같은 손실을 두 번 셈)"""
    cfg = live_cfg(tmp_path)
    d2 = US.now().date()
    d1 = prev_weekday(d2)
    sup, br = await fresh(cfg)
    e = sup.engine
    clock = [US.epoch(d1, 9, 0)]
    br.now = lambda: clock[0]
    br.daily = {s: _closes(s, prev_weekday(d1), [100.0] * 30) for s in SYMS}
    await sup.day_start(d1)
    await open_book(sup, br, d1)
    e.on_fill(Fill(SYMS[0], SELL, 30, 96.0, e.now, reason="stop_loss"))          # 첫날: 손절 1건
    br.holdings[SYMS[0]] = 0
    feed(e, d1, 15, 59, 99.0)
    clock[0] = US.epoch(d1, 16, 10)
    await sup.day_end(d1)
    first = sup.days[-1]
    assert first["day"] == str(d1) and first["n"] == 1 and first["pnl"] < -100

    clock[0] = US.epoch(d2, 9, 0)
    br.daily = {s: _closes(s, d1, [100.0] * 29 + [99.0]) for s in SYMS}
    await sup.day_start(d2)
    clock[0] = US.epoch(d2, 16, 10)                          # 둘째 날: 시세가 한 건도 들어오지 않았다
    await sup.day_end(d2)
    second = sup.days[-1]
    assert second["day"] == str(d2) and second["n"] == 0 and second["wins"] == 0 and second["pnl"] == 0
    assert abs(second["equity"] - first["equity"]) < 0.01
    await asyncio.to_thread(sup.journal.flush)
    rows = {r["day"]: r for r in sup.journal.load_daily(5)}
    assert rows[str(d2)]["n"] == 0 and rows[str(d2)]["pnl"] == 0 and rows[str(d1)]["n"] == 1
    sup.journal.close()


# ---------------------------------------------------------------------- 감시 목록에서 빠졌다 돌아온 종목
def _weekdays(end: date, n: int) -> list:
    out, x = [], end
    while len(out) < n:
        if x.weekday() < 5:
            out.append(x)
        x -= timedelta(days=1)
    out.reverse()
    return out


class DailyStub(Broker):
    """일봉만 주는 증권사 흉내: '오늘' 이전의 봉만 돌려준다(장전 조회와 같다)."""

    def __init__(self, series):
        super().__init__()
        self.series, self.t, self.day0 = series, 0.0, 0.0

    def now(self):
        return self.t

    async def get_daily_bars(self, symbol, n):
        return [b for b in self.series[symbol] if b.ts < self.day0][-n:]


def _watch_env(tmp_path, days):
    series = {}
    for k, sym in enumerate(("AAA", "BBB")):
        series[sym] = [Bar(sym, US.epoch(d, 9, 30), 100.0 + k + i * 0.3, 101.0 + k + i * 0.3, 99.5 + k + i * 0.3,
                           100.4 + k + i * 0.3 + (0.9 if i % 7 == 3 else 0.0), 1_000_000 + 1000 * i)
                       for i, d in enumerate(days)]
    cfg = Config.for_market("US")
    cfg.data_dir = str(tmp_path)
    cfg.universe = ["AAA", "BBB"]
    cfg.llm.enabled = cfg.news.enabled = cfg.learn.enabled = False
    cfg.risk.max_positions = 0                               # 주문은 내지 않는다(지표·판단 대기 상태만 본다)
    br = DailyStub(series)
    sup = Supervisor(cfg, br)
    return cfg, sup, br, series


async def _run_day(sup, br, series, i, d, hh=9, end=True):
    br.t, br.day0 = US.epoch(d, hh, 0), US.epoch(d)
    await sup.day_start(d)
    e = sup.engine
    for h2, m2, key in ((9, 31, "open"), (11, 30, "low"), (13, 0, "high"), (15, 59, "close")):
        if h2 < hh:
            continue
        for sym in sup.symbols:
            b = series[sym][i]
            e.on_tick(Tick(sym, US.epoch(d, h2, m2), getattr(b, key), b.volume // 4))
    if end:
        br.t = US.epoch(d, 16, 10)
        await sup.day_end(d)


async def test_symbol_returning_to_the_watch_list_is_warmed_again(tmp_path):
    """감시 목록에서 며칠 빠졌다 돌아온 종목은 지표를 이력으로 다시 채우고, 빠지기 전의 '판단 대기' 표시는 버린다.
    (이전: 한 번 예열한 종목은 다시 채우지 않음 → 돌아온 날 몇 주 전 봉을 기준으로 오늘 가격에 매수 판단,
           그 뒤로도 지표에 빈 구간이 남음)"""
    for hh, early in ((9, True), (11, False)):
        days = _weekdays(date(2026, 3, 13), 90)
        cfg, sup, br, series = _watch_env(tmp_path / f"h{hh}", days)
        e = sup.engine
        n = len(days)
        for i in range(n - 8, n - 6):                        # 이틀 동안 두 종목 모두 감시
            await _run_day(sup, br, series, i, days[i])
        last_seen = days[n - 7]
        s = e.sym["BBB"]
        assert US.local(s.hist[-1][0]).date() == last_seen and s.undecided
        cfg.universe = ["AAA"]                               # BBB 가 감시 목록에서 빠진다(거래대금 순위 변동 등)
        for i in range(n - 6, n - 1):
            await _run_day(sup, br, series, i, days[i])
        assert "BBB" not in sup.symbols and US.local(s.hist[-1][0]).date() == last_seen
        cfg.universe = ["AAA", "BBB"]                        # 닷새 뒤 다시 들어온다
        br.t, br.day0 = US.epoch(days[-1], hh, 0), US.epoch(days[-1])
        await sup.day_start(days[-1])
        assert US.local(s.hist[-1][0]).date() == days[-2]    # 어제 봉까지 채워졌다
        assert s.undecided == early                          # 장 초반이면 어제 봉을 판단하고, 장중이면 낡은 표시를 지운다
        ref = Engine(cfg, StrategyParams(), 1.0, quiet=True)
        ref.warm("BBB", series["BBB"][:-1][-3 * cfg.strategy.warmup_bars:])
        r = ref.sym["BBB"]
        assert (s.f.ema_fast, s.f.ema_slow, s.f.atr) == (r.f.ema_fast, r.f.ema_slow, r.f.atr)
        assert [h[0] for h in s.hist] == [h[0] for h in r.hist]              # 중간에 빈 날이 없다
        sup.journal.close()


async def test_day_that_was_never_closed_is_not_counted_twice_after_rewarming(tmp_path):
    """장 마감 처리를 못 하고 다음 날이 됐을 때(절전 등) 어제의 봉을 두 번 넣지 않는다.
    — 이력으로 다시 채웠으면, 닫지 못한 채 남아 있던 어제의 반쪽 봉은 버린다."""
    days = _weekdays(date(2026, 3, 13), 90)
    cfg, sup, br, series = _watch_env(tmp_path, days)
    e = sup.engine
    n = len(days)
    await _run_day(sup, br, series, n - 3, days[n - 3])
    br.t, br.day0 = US.epoch(days[n - 2], 9, 0), US.epoch(days[n - 2])
    await sup.day_start(days[n - 2])
    e.on_tick(Tick("BBB", US.epoch(days[n - 2], 9, 31), 150.0, 10))        # 어제: 시세 한 건만 받고 잠들었다(마감 처리 없음)
    s = e.sym["BBB"]
    assert s.bar_start >= 0.0
    br.t, br.day0 = US.epoch(days[-1], 9, 0), US.epoch(days[-1])
    await sup.day_start(days[-1])
    e.on_tick(Tick("BBB", US.epoch(days[-1], 9, 31), series["BBB"][-1].open, 10))
    got = [h for h in s.hist if US.local(h[0]).date() == days[n - 2]]
    assert len(got) == 1 and got[0][4] == series["BBB"][n - 2].close       # 어제 봉은 하나, 값은 증권사 일봉(반쪽 봉 150 이 아니다)
    assert US.local(s.hist[-1][0]).date() == days[n - 2] and s.bar_start == US.epoch(days[-1], 9, 30)
    sup.journal.close()


# ====================================================================== 봉 재생
def test_replay_keeps_bars_whose_bucket_starts_before_the_open():
    """봉의 시작 시각이 장 시작보다 이른 봉(미국 1시간봉의 09:00 봉, 날짜만 있는 일봉)도 재생에서 빠지지 않는다.
    (이전: 봉 안의 다섯 점이 모두 장 시작 전이라 엔진이 버림 → 날마다 첫 봉이 통째로 사라짐)"""
    cfg = Config.for_market("US")
    cfg.strategy.bar_sec = 3600
    rec = []
    e = Engine(cfg, StrategyParams(), 30_000.0, quiet=True)
    e.on_bar_cb = rec.append
    for d in (date(2026, 1, 5), date(2026, 1, 6)):
        for m in range(0, 390, 5):
            e.on_tick(Tick("X", US.epoch(d, 9, 30) + m * 60, 100 + m * 0.01, 100))
        e.flush_bars()
    assert len(rec) == 14 and US.local(rec[0].ts).strftime("%H:%M") == "09:00"
    bt = Backtester(cfg, StrategyParams())
    got = []
    bt.engine.on_bar_cb = got.append
    bt.run_bars(list(rec))
    assert [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in got] == \
           [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in rec]

    cfg = Config.for_market("US")                            # 일봉인데 시각이 '그날 0시'로 찍힌 자료
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    bars = [Bar("X", US.epoch(d), 100.0, 101.0, 99.0, 100.5, 1000) for d in days]
    bt = Backtester(cfg, StrategyParams())
    bt.run_bars(bars)
    assert bt.engine.sym["X"].bar_count == 5


# ====================================================================== 소식
SLASH = "Nvidia misses estimates and slashes guidance"


async def test_news_published_while_the_bot_was_off_is_still_judged_for_held_symbols():
    """프로그램이 꺼져 있던 사이(밤사이·주말)에 나온 보유 종목 소식도 켜진 뒤 Claude 판정을 받는다.
    (이전: 처음 봤을 때 90분이 지나 있으면 영영 판정하지 않음 → 켤 때마다 밤사이 악재를 통째로 버림)"""
    llm = SymLLM({"NVDA": -2})
    cfg, e, na = news_env2(llm, held=("NVDA",))
    now = NOW + 3 * 3600                                     # 발행 3시간 뒤에 켜졌다
    na.ingest([(NOW, SLASH, "", "x", ()), (NOW, "Tesla recalls two million cars over brake defect", "", "x", ())], now)
    assert len(na.items) == 2 and not any(it.fresh for it in na.items.values())
    assert "NVDA" not in e.news                              # 묵은 소식의 규칙 점수는 반영하지 않는다(그대로)
    assert await na.classify(now + 400) == 1
    assert [sym for _, sym, _ in llm.calls[0]["items"]] == ["NVDA"]     # 보유하지 않은 종목의 묵은 소식은 묻지 않는다
    assert e.exit_now == {"NVDA"}
    assert e.news["NVDA"].score == -1.0 and e.news["NVDA"].block_until > now      # 판정 전체가 반영된다


async def test_late_news_is_capped_per_symbol_and_untimed_broker_backlog_is_never_judged():
    """뒤늦게 본 소식은 보유 종목마다 최신 몇 건만 묻는다(켤 때마다 며칠 치 제목을 전부 묻지 않는다).
    증권사가 시작 직후 준 묵은 목록(발행 시각을 믿을 수 없다)은 보유 종목 것이어도 판정하지 않는다."""
    llm = SymLLM(default=0)
    cfg, e, na = news_env2(llm, held=("NVDA",))
    now = NOW + 3 * 3600
    na.ingest([(NOW - i * 600, f"Nvidia headline number {i}", "", "x", ()) for i in range(20)], now)
    assert await na.classify(now + 400) == na.LATE_PER_SYM
    asked = [t for _, _, t in llm.calls[0]["items"]]
    assert len(asked) == na.LATE_PER_SYM and asked[0] == "Nvidia headline number 0"     # 최신 것부터
    assert await na.classify(now + 800) == 0 and len(llm.calls) == 1                    # 더 오래된 것은 묻지 않는다

    llm = SymLLM({"NVDA": -2})
    cfg, e, na = news_env2(llm, held=("NVDA",))
    na.ingest([(NOW, SLASH, "", "KIS", ("NVDA",), True)], NOW)                          # 여섯째 값 = 묵은 목록
    it = next(iter(na.items.values()))
    assert it.stale and not it.fresh
    assert await na.classify(NOW + 400) == 0 and not llm.calls and not e.exit_now and "NVDA" not in e.news


async def test_claude_verdicts_are_reused_after_a_restart(tmp_path):
    """이전 실행에서 받은 Claude 판정은 일지에서 이어받아 다시 묻지 않는다(토큰 절약)."""
    j = Journal(tmp_path / "journal.db")
    llm = SymLLM({"NVDA": -1, "AMD": 1})
    cfg, e, na = news_env2(llm, held=("NVDA", "AMD"))
    na.journal = j
    title = "Nvidia and AMD face new export limits"
    na.ingest([(NOW, title, "", "x", ())], NOW)
    assert await na.classify(NOW + 400) == 2
    j.flush()

    llm2 = SymLLM({"NVDA": -2, "AMD": -2})
    cfg, e2, na2 = news_env2(llm2, held=("NVDA", "AMD"))
    na2.journal = j                                          # 다시 켜졌다(같은 일지)
    na2.ingest([(NOW, title, "", "x", ()), (NOW, SLASH, "", "x", ())], NOW + 3 * 3600)
    it = next(x for x in na2.items.values() if x.title == title)
    assert it.verdicts == {"NVDA": -1, "AMD": 1} and it.impact == -1
    assert await na2.classify(NOW + 3 * 3600 + 400) == 1
    assert [(sym, t) for _, sym, t in llm2.calls[0]["items"]] == [("NVDA", SLASH)]      # 새 소식만 묻는다
    j.close()


def test_journal_adds_the_verdict_column_to_an_older_database(tmp_path):
    """이전 버전이 만든 일지(소식 표에 판정 열이 없음)도 그대로 이어 쓴다."""
    p = tmp_path / "journal.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE news(id TEXT PRIMARY KEY, ts REAL, source TEXT, symbols TEXT, title TEXT, url TEXT, "
                "rule REAL, impact INTEGER, kind TEXT)")
    con.execute("INSERT INTO news VALUES('old', 5.0, 'x', 'NVDA', 't', '', 0.0, -2, 'legal')")
    con.commit()
    con.close()
    for _ in range(2):                                       # 두 번 열어도 문제없다
        j = Journal(p)
        llm = SymLLM({"NVDA": -2})
        cfg, e, na = news_env2(llm, held=("NVDA",))
        na.journal = j
        na.ingest([(NOW, SLASH, "", "x", ())], NOW)
        j.flush()
        j.close()
    j = Journal(p)
    assert j.news_verdicts(0.0) == {}                        # 판정이 없는 행(옛 행 포함)은 나오지 않는다
    it = next(iter(na.items.values()))
    it.judge("NVDA", -2, "earnings")
    j.news(it)
    j.flush()
    assert j.news_verdicts(0.0) == {it.id: ({"NVDA": -2}, "earnings")}
    j.close()


# ====================================================================== 주문 처리(네 번째 검토)
import json
import time
from datetime import datetime
from email.utils import formatdate
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web

from aitrader.brokers.base import BrokerError, BrokerUnknown
from aitrader.brokers.kis_overseas import KISOverseasBroker
from aitrader.brokers.manual import ManualBroker
from aitrader.executor import MAX_CANCEL_FAILS, Executor
from aitrader.models import BUY, Order, OrderIntent

from test_kis_overseas import os_cfg
from test_review2 import Acct, FakeOS2


async def test_late_confirmed_buy_gets_its_exit_ticket_without_waiting_for_the_snooze(tmp_path):
    """주문 보조: 만료된 매수 제안을 '사실은 체결됨'으로 늦게 확인한 직후에도 손절 제안은 바로 뜬다.
    (이전: 제안이 만료될 때 걸린 '다시 제안하지 않는 30분'이 그대로 남아, 방금 산 종목이 손절가 아래로
           급락해도 30분 동안 매도 제안이 하나도 뜨지 않음 — 전량 청산·악재 청산도 같은 문에 막힘)"""
    cfg = Config.for_market("US")
    cfg.mode, cfg.data_dir = "manual", str(tmp_path)
    cfg.llm.enabled = cfg.news.enabled = cfg.learn.enabled = False
    cfg.universe = ["SPY"]
    br = ManualBroker(cfg, base_url="http://127.0.0.1:9")
    sup = Supervisor(cfg, br)
    e = sup.engine
    t0 = US.epoch(date(2026, 1, 5), 10, 0)
    br.last["SPY"] = 500.0
    e.on_tick(Tick("SPY", t0, 500.0, 100, 499.9, 500.1))
    e._send(OrderIntent("SPY", BUY, 10, 500.0, "entry", {"stop_dist": 10.0, "atr": 5.0, "signal_px": 500.0}), e.sym["SPY"])
    await asyncio.sleep(0.01)
    t1 = list(br.tickets.values())[-1]
    sup.executor.on_timer(sup.executor.clock() + cfg.manual.ticket_ttl_min * 60 + 1)    # 5분 안에 답하지 못해 만료
    await asyncio.sleep(0.01)
    assert t1["status"] == "EXPIRED" and e.sym["SPY"].retry_after - e.now > 1000
    ok, _ = br.confirm(t1["id"], 10, 500.0)                 # 사실은 샀다 → 늦은 체결 확인
    assert ok and e.positions["SPY"].stop == 490.0 and e.sym["SPY"].retry_after == 0.0
    br.last["SPY"] = 465.0
    e.on_tick(Tick("SPY", t0 + 60, 465.0, 100, 464.9, 465.1))           # 1분 뒤 손절가 아래로 급락
    await asyncio.sleep(0.01)
    assert [(t["status"], t["reason"]) for t in br.tickets.values() if t["side"] == SELL] == [("OPEN", "stop_loss")]
    sup.journal.close()


# ---------------------------------------------------------------------- 재시작 정리(recover)
WALL = US.epoch(date(2026, 3, 11), 11, 0)       # 집행기가 보는 '실제 시각'을 장중(수요일 11:00 뉴욕)으로 고정한다
AFTER_CLOSE = US.epoch(date(2026, 3, 11), 17, 0)


async def _crash_with_live_buy(tmp_path):
    """매수 주문 1건이 증권사에 살아 있는 채로 프로세스가 죽은 상태를 만든다. (설정, 이전 증권사 흉내)"""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    sup.executor.wall = lambda: WALL
    e = sup.engine
    e.on_tick(Tick(SYMS[0], US.epoch(US.now().date(), 10, 0), 100.0, 10, 99.9, 100.1))
    e._send(OrderIntent(SYMS[0], BUY, 10, 100.0, "entry", {"stop_dist": 3.0, "atr": 1.5}), e.sym[SYMS[0]])
    await asyncio.sleep(0.05)
    st = json.loads((sup.dir / "state.json").read_text())
    assert [(d["symbol"], d["status"], d["broker_id"]) for d in st["orders"]] == [(SYMS[0], "ACK", "1")]
    sup.journal.close()
    return cfg, br


def _reborn(cfg, old, poll=None):
    br = Acct(old.cash)
    br.holdings, br.orders, br.n, br.daily = dict(old.holdings), old.orders, old.n, old.daily
    if poll is not None:
        br.poll_order = poll(br.poll_order)
    sup = Supervisor(cfg, br)
    sup.executor.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
    sup.executor.wall = lambda: WALL + 60
    return sup, br


async def test_recover_retries_a_failed_poll_instead_of_forgetting_the_order(tmp_path):
    """재시작 직후 주문 상태 조회가 한 번 실패해도 그 주문을 잊지 않는다 — 다시 조회해서 취소까지 마친다.
    (이전: 이벤트 한 줄만 남기고 넘어감 → 살아 있는 주문이 추적도 취소도 안 된 채 남고, 나중에 체결되면
           계좌에는 있는데 장부에는 없는 주식이 됨)"""
    cfg, old = await _crash_with_live_buy(tmp_path)
    calls = [0]

    def flaky(real):
        async def poll(order):
            calls[0] += 1
            if calls[0] == 1:
                raise asyncio.TimeoutError()
            return await real(order)
        return poll

    sup, br = _reborn(cfg, old, flaky)
    await sup.reconcile(initial=True)
    assert calls[0] >= 2 and br.orders["1"]["done"]         # 다시 조회했고, 살아 있던 주문은 취소됐다
    assert not sup.executor.orders and not sup.engine.risk.halted and not sup.engine.pending
    sup.journal.close()


async def test_recover_keeps_an_unverifiable_order_locked_and_halts(tmp_path):
    """끝내 상태를 확인하지 못한 이전 주문은 '확인 불가' 주문으로 남긴다: 종목 잠금 + 신규 매수 정지 + 상태 저장.
    뒤늦게 온 체결 통보는 그 주문으로 장부에 반영되고, 사람이 [재개]를 누르면 잠금이 풀린다."""
    cfg, old = await _crash_with_live_buy(tmp_path)

    def dead(real):
        async def poll(order):
            raise asyncio.TimeoutError()
        return poll

    sup, br = _reborn(cfg, old, dead)
    await sup.reconcile(initial=True)
    e, ex = sup.engine, sup.executor
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and o.broker_id == "1" and SYMS[0] in e.pending and e.risk.halted
    assert any("이전 실행의" in ev["text"] and "계좌를 직접 확인" in ev["text"] for ev in e.events if ev["kind"] == "risk")
    st = json.loads((sup.dir / "state.json").read_text())
    assert [(d["status"], d["broker_id"]) for d in st["orders"]] == [("STUCK", "1")]      # 다음 시작 때 다시 확인한다
    br.fill("1")                                            # 그 주문이 나중에 체결된다(통보가 온다)
    await asyncio.sleep(0.05)
    assert e.positions[SYMS[0]].qty == 10 and not ex.orders and SYMS[0] not in e.pending
    sup.journal.close()

    # 같은 상황에서 체결 없이 사람이 계좌를 확인하고 [재개]
    cfg2, old2 = await _crash_with_live_buy(tmp_path / "b")
    sup2, br2 = _reborn(cfg2, old2, dead)
    await sup2.reconcile(initial=True)
    assert sup2.engine.risk.halted and sup2.executor.orders
    sup2.notifier.send = lambda text: None
    sup2.control("resume")
    assert not sup2.executor.orders and not sup2.engine.pending and not sup2.engine.risk.halted
    sup2.journal.close()


class _Stale(Broker):
    """취소 요청이 실패하는 증권사. 조회는 되고, 기본은 '체결 없음·진행 중'(끝났다는 표시가 없다)을 돌려준다.
    cancel_exc=BrokerError: 명시적 거부. cancel_exc=TimeoutError: 취소 요청이 닿았는지조차 알 수 없음.
    cum/done 을 바꿔 '사실은 체결돼 있었다'·'증권사가 끝난 주문이라고 한다'를 흉내 낸다."""
    supports_poll = True

    def __init__(self, lookup="unknown", cancel_exc=BrokerError):
        super().__init__()
        self.cancels = self.polls = self.lookups = 0
        self.lookup, self.cancel_exc = lookup, cancel_exc
        self.cum, self.done = 0, False
        self.cancel_ok = False

    async def place_order(self, order):
        order.broker_id = "1"

    async def cancel_order(self, order):
        self.cancels += 1
        if self.cancel_ok:
            self.done = True
            return
        raise self.cancel_exc("취소할 수 없습니다")

    async def poll_order(self, order):
        self.polls += 1
        return self.cum, 166.0 if self.cum else 0.0, self.done

    async def lookup_order(self, order):
        self.lookups += 1
        return self.lookup, ""


def _saved(created, broker_id="1", status="ACK"):
    return [{"id": 1, "symbol": "NVDA", "side": "BUY", "qty": 10, "limit_price": 166.0, "reason": "entry",
             "ctx": {"stop_dist": 4.0}, "created_ts": created, "wall_ts": created, "status": status,
             "broker_id": broker_id, "broker_org": "", "filled": 0, "avg_price": 0.0}]


async def test_recover_does_not_drop_orders_it_could_not_cancel_or_find():
    """재시작 정리: 끝났다고 확인하지 못한 주문은 잊지도, 끝났다고 단정하지도 않는다.
      · 취소가 안 되고(거부든 응답 없음이든) 증권사의 '끝남' 표시도 없는 오늘 주문 → 잠가 두고 사람을 부른다.
        (취소 거부를 '취소할 것이 없다 = 끝났다'로 읽으면, 호출 한도 같은 사유로 거부된 살아 있는 주문을 놓친다)
      · 증권사가 끝난 주문이라고 하면 조용히 끝낸다. 그 주문을 낸 날의 장이 이미 끝났어도 마찬가지(당일 주문은 소멸).
      · 접수 여부를 알 수 없으면 잠그고, 증권사에 닿지 않은 것이 확인되면 정리한다."""
    cfg = Config.for_market("US")

    def env(br, wall=WALL):
        e = Engine(cfg, StrategyParams(), 30_000)
        ex = Executor(br, e, 10.0)
        ex.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
        ex.follow_delays = (0.01, 0.02)
        ex.stuck_poll_sec = 1e9                              # 이 테스트에서는 자동 재확인을 돌리지 않는다
        ex.wall = lambda: wall
        return e, ex

    for exc in (asyncio.TimeoutError, BrokerError):         # 취소가 닿았는지 모름 / 명시적 거부 — 어느 쪽도 '끝남'의 증거가 아니다
        br = _Stale(cancel_exc=exc)
        e, ex = env(br)
        assert await ex.recover(_saved(WALL - 60)) == 0
        assert br.cancels == 1 and [o.status for o in ex.orders.values()] == ["STUCK"], exc
        assert e.risk.halted and "NVDA" in e.pending and ex.unsettled() == {"NVDA"}

    br = _Stale()                                           # 취소는 거부됐지만 증권사가 끝난 주문이라고 한다(이미 취소돼 있었다)
    br.done = True
    e, ex = env(br)
    assert await ex.recover(_saved(WALL - 60)) == 0
    assert not ex.orders and not e.risk.halted and not e.pending and ex.export() == []

    br = _Stale(cancel_exc=asyncio.TimeoutError)
    e, ex = env(br)
    assert await ex.recover(_saved(WALL - 4 * 86400)) == 0  # 나흘 전에 낸 주문(당일 주문이라 이미 소멸)
    assert br.cancels == 1 and not ex.orders and not e.risk.halted and not e.pending
    ex.shutdown()

    br = _Stale()
    e, ex = env(br, wall=AFTER_CLOSE)                       # 오늘 낸 주문이지만 장이 이미 끝났다
    assert await ex.recover(_saved(WALL - 60)) == 0
    assert br.cancels == 1 and not ex.orders and not e.risk.halted and not e.pending
    ex.shutdown()

    br = _Stale(lookup="unknown")                           # 전송 도중 꺼져 주문번호 없이 저장됨 + 조회로도 알 수 없음
    e, ex = env(br)
    assert await ex.recover(_saved(WALL - 60, broker_id="", status="SENT")) == 0
    assert br.lookups >= 2 and [o.status for o in ex.orders.values()] == ["STUCK"] and e.risk.halted

    br = _Stale(lookup="absent")                            # 두 번 연속 '없음' → 증권사에 닿지 않은 주문
    e, ex = env(br)
    assert await ex.recover(_saved(WALL - 60, broker_id="", status="SENT")) == 0
    assert br.lookups == 2 and not ex.orders and not e.risk.halted and not e.pending


async def test_cancel_failure_is_not_taken_as_proof_that_the_order_ended():
    """취소 요청이 실패했을 때: 끝없이 되풀이하지도, '끝난 주문'이라고 단정하지도 않는다.
    (처음: 10초마다 취소를 무한 반복 — 종목 잠금·계좌 대사 중단이 조용히 계속되고 경보가 없음.
     그다음 고친 것: 명시적 거부를 '취소할 것이 없다'로 읽고 수량이 그대로면 끝낸 것 → 호출 한도로 거부된 살아 있는
     주문의 잠금이 풀려 같은 종목에 새 주문이 나가고, 둘 다 체결되면 수량이 두 배)"""
    cfg = Config.for_market("US")
    t0 = US.epoch(date(2026, 3, 11), 10, 59)

    async def run(br, cycles=40):
        e = Engine(cfg, StrategyParams(), 30_000)
        clock = [1000.0]
        ex = Executor(br, e, cfg.risk.order_timeout_sec, clock=lambda: clock[0])
        ex.settle_delays = (0.0, 0.002, 0.002, 0.002, 0.002, 0.002)
        ex.follow_delays = (0.002,)
        ex.stuck_poll_sec = 1e9                              # 자동 재확인은 아래에서 따로 본다
        ex.wall = lambda: WALL
        e.on_tick(Tick("NVDA", t0, 166.0, 1, 165.9, 166.1))
        e.on_fill(Fill("NVDA", BUY, 10, 166.0, t0, ctx={"stop_dist": 4.0}))
        e._send(OrderIntent("NVDA", SELL, 10, 0.0, "stop_loss"), e.sym["NVDA"])
        await asyncio.sleep(0.01)
        o = next(iter(ex.orders.values()))
        made = o.created_ts
        for _ in range(cycles):
            clock[0] += cfg.risk.order_timeout_sec + 0.5
            ex.on_timer(clock[0])
            await asyncio.sleep(0.004)
        assert o.created_ts == made                         # 주문을 낸 시각은 건드리지 않는다(주문 내역과 견주는 기준)
        return e, ex, o, clock

    # 1) 취소가 닿았는지 모르는 실패가 이어진다 / 2) 명시적으로 거부되는데 조회에는 아직 열려 있다 → 세 번 만에 멈추고 사람을 부른다
    for exc in (asyncio.TimeoutError, BrokerError):
        br = _Stale(cancel_exc=exc)
        e, ex, o, clock = await run(br)
        assert o.status == "STUCK" and br.cancels == MAX_CANCEL_FAILS and e.risk.halted and "NVDA" in e.pending, exc
        assert sum(1 for ev in e.events if ev["kind"] == "risk") == 1
        # 확인 불가 주문은 주기적으로 다시 본다: 아직 살아 있으면 취소를 다시 보내고, 접수되면 정리된다
        ex.stuck_poll_sec = 0.0
        br.cancel_ok = True                                 # 막혀 있던 사유(호출 한도 등)가 풀렸다
        ex.on_timer(clock[0])
        await asyncio.sleep(0.05)
        assert br.cancels == MAX_CANCEL_FAILS + 1 and not ex.orders and "NVDA" not in e.pending and e.risk.halted
        ex.shutdown()

    # 3) 거부된 까닭이 '이미 끝난 주문'이었다(증권사가 끝남 표시를 준다) → 그 자리에서 끝내고 멈추지 않는다
    br = _Stale()
    br.done = True
    e, ex, o, clock = await run(br, cycles=3)
    assert o.status == "CANCELED" and br.cancels == 1 and not ex.orders and not e.risk.halted and "NVDA" not in e.pending
    ex.shutdown()

    # 4) 거부된 까닭이 '취소보다 먼저 전량 체결'이었다 → 체결을 반영하고 끝(평범한 상황 — 정지 없음)
    br = _Stale()
    br.cum = 10
    e, ex, o, clock = await run(br, cycles=3)
    assert o.status == "FILLED" and br.cancels == 1 and "NVDA" not in e.positions and not e.risk.halted
    ex.shutdown()

    # 5) 확인 불가로 남은 주문도, 그 주문을 낸 날의 장이 끝나면 소멸한 것으로 정리한다(다음 날까지 잠겨 있지 않게)
    br = _Stale(cancel_exc=asyncio.TimeoutError)
    e, ex, o, clock = await run(br)
    assert o.status == "STUCK"
    ex.stuck_poll_sec = 0.0
    ex.wall = lambda: AFTER_CLOSE
    ex.on_timer(clock[0])
    await asyncio.sleep(0.05)
    assert o.status == "CANCELED" and not ex.orders and "NVDA" not in e.pending and e.risk.halted
    ex.shutdown()


class _Slow(Broker):
    """취소가 '접수'만 되고 주문은 잠깐 더 살아 있는 증권사(그 사이 체결될 수 있다)."""
    supports_poll = True

    def __init__(self):
        super().__init__()
        self.cum, self.done = 0, False

    async def place_order(self, order):
        order.broker_id = "1"

    async def cancel_order(self, order):
        return None

    async def poll_order(self, order):
        return self.cum, 10_000.0 if self.cum else 0.0, self.done


async def test_cancelled_order_still_being_watched_is_saved_and_rechecked_after_restart():
    """취소로 끝낸 뒤 뒤늦은 체결을 지켜보던 주문은, 그 상태로 꺼지면 저장해 두었다가 다음 시작 때 한 번 더 조회한다.
    (이전: 종료 절차가 '수량이 두 번 같다'로 주문을 끝내고 저장 대상에서 빼 버려, 취소 직전에 체결된 4주가
           영영 장부에 들어오지 않음)"""
    def env(br):
        e = Engine(Config(), StrategyParams(), 10_000_000)
        ex = Executor(br, e, 10.0)
        ex.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
        ex.follow_delays = (0.05, 0.1, 0.2)
        e.on_tick(Tick("A", US.epoch(date(2026, 1, 5), 10, 0), 10_000.0, 1))
        return e, ex

    br = _Slow()
    e, ex = env(br)
    it = OrderIntent("A", BUY, 10, 10_000.0, "entry", {"stop_dist": 300.0})
    e.pending["A"] = it
    ex.submit(it)
    await asyncio.sleep(0.01)
    await ex.cancel_all()                                   # 종료 절차(Supervisor.run 의 마무리와 같은 순서)
    assert not ex.orders and [d["status"] for d in ex.export()] == ["CANCELED"]
    br.cum, br.done = 4, True                               # 취소가 처리되기 직전에 4주가 체결됐다
    ex.shutdown()
    saved = ex.export()
    assert [(d["status"], d["broker_id"], d["filled"]) for d in saved] == [("CANCELED", "1", 0)]

    e2, ex2 = env(br)
    assert await ex2.recover(saved) == 1
    assert e2.positions["A"].qty == 4 and not ex2.orders and not e2.risk.halted

    # 꺼지지 않고 계속 돌았다면: 추적 조회가 끝난 주문은 더 이상 저장하지 않는다
    br3 = _Slow()
    e3, ex3 = env(br3)
    e3.pending["A"] = it
    ex3.submit(it)
    await asyncio.sleep(0.01)
    await ex3.cancel_all()
    await asyncio.sleep(0.5)
    assert ex3.export() == []
    ex3.shutdown()


async def test_settle_does_not_count_answers_that_are_behind_the_ledger():
    """취소 확정 조회에서 '이미 장부에 넣은 수량보다 적은' 답은 확인으로 세지 않는다(그 주문의 최신 상태가 아니다).
    (이전: 주문 행을 못 찾아 돌려준 '0주·진행 중' 두 번을 '수량이 더 변하지 않는다'로 보고 취소를 확정)"""
    br = _Slow()
    e = Engine(Config(), StrategyParams(), 10_000_000)
    ex = Executor(br, e, 10.0)
    ex.settle_delays = (0.0, 0.01, 0.01, 0.01)
    e.on_tick(Tick("A", US.epoch(date(2026, 1, 5), 10, 0), 10_000.0, 1))
    it = OrderIntent("A", BUY, 10, 10_000.0, "entry", {"stop_dist": 300.0})
    e.pending["A"] = it
    ex.submit(it)
    await asyncio.sleep(0.01)
    ex.on_fill("1", 4, 10_000.0, time.time())               # 통보로 4주 체결을 이미 반영했다
    assert e.positions["A"].qty == 4
    br.cum = 0                                              # 그런데 조회는 계속 '0주'라고 한다(행을 못 찾는 상황)
    await ex.cancel_all()
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and e.risk.halted and "A" in e.pending
    ex.shutdown()


# ---------------------------------------------------------------------- 해외 어댑터: 주문 찾기
@pytest.fixture
async def osx(tmp_path, unused_tcp_port):
    fake = FakeOS2()
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    cfg = os_cfg(tmp_path)
    made = []

    async def broker():
        br = KISOverseasBroker(cfg, f"http://127.0.0.1:{unused_tcp_port}", f"ws://127.0.0.1:{unused_tcp_port}")
        br.rl.interval = 0.0
        await br.start()
        br.last["NVDA"] = Tick("NVDA", 0.0, 166.19, 1, 166.18, 166.20)
        made.append(br)
        return br

    yield fake, broker
    for br in made:
        await br.stop()
    await runner.cleanup()


def _order(n, qty=7, at=None):
    t = time.time() if at is None else at
    return Order(n, "NVDA", BUY, qty, 166.25, "entry", {}, t, wall_ts=t)


async def test_overseas_poll_says_unknown_when_the_order_row_is_missing(osx):
    """주문체결내역에 그 주문이 (아직) 없으면 '0주 체결·진행 중'을 지어내지 않고 '모름'으로 올린다.
    (이전: 지어낸 답 두 번이 '수량이 더 변하지 않는다'로 세어져 취소가 확정되고 잠금이 풀림 — 남은 수량이 그 뒤에
           체결되면 장부에 들어오지 않음)"""
    fake, broker = osx
    br = await broker()
    o = _order(1)
    await br.place_order(o)
    fake.hidden = {1}
    with pytest.raises(BrokerUnknown):
        await br.poll_order(o)
    fake.hidden = set()
    assert await br.poll_order(o) == (7, 166.25, True)


async def test_overseas_lookup_is_not_fooled_by_an_older_identical_order(osx):
    """접수 여부를 모르는 주문을 찾을 때, 같은 종목·방향·수량의 '예전 주문'을 그 주문으로 착각하지 않는다.
    (이전: '아는 주문' 목록이 프로세스마다 비어 있고 시간 조건도 없어, 재시작 뒤에는 어제 체결된 같은 주문에
           붙어 그 체결을 한 번 더 장부에 넣음 — 계좌에 없는 주식이 장부에 생김)"""
    fake, broker = osx
    br1 = await broker()
    await br1.place_order(_order(1))                        # 이전 실행: NVDA 7주 매수(접수·체결, 장부에 반영 끝)
    state = br1.export_state()
    assert list(state["known"]) == ["30100001"]

    # 1) 재시작: 이 봇이 냈던 주문번호를 이어받는다 → 그 주문은 후보가 아니다
    br2 = await broker()
    br2.import_state(state)
    assert await br2.lookup_order(_order(2)) == ("absent", "")
    # 같은 프로세스에서도 마찬가지
    assert await br1.lookup_order(_order(2)) == ("absent", "")

    # 2) 주문번호를 이어받지 못했더라도(상태 파일 유실), 이틀 전에 접수된 주문은 시각으로 걸러진다 — 시각 필드가
    #    현지 시각이든 한국 시각이든 마찬가지
    for kst in (False, True):
        fake.tmd_kst = kst
        fake.times[0] = time.time() - 2 * 86400
        br3 = await broker()
        assert await br3.lookup_order(_order(3)) == ("absent", "")
    fake.tmd_kst = False

    # 3) 방금 접수된 모르는 주문이 하나면 그것이 이 주문이다
    fake.times[0] = time.time() - 2 * 86400
    probe = _order(4)
    fake.gateway_error = True                               # 접수는 됐지만 응답이 게이트웨이 오류 화면
    br4 = await broker()
    with pytest.raises(BrokerUnknown):
        await br4.place_order(probe)
    fake.gateway_error = False
    assert await br4.lookup_order(probe) == ("found", "30100002")

    # 4) 후보가 둘이면(사람이 같은 주문을 직접 또 냈다) 고르지 않는다 → 'unknown'(집행기가 잠그고 사람을 부른다)
    br5 = await broker()
    await br5.place_order(_order(5))                        # 다른 누군가의 같은 주문(이 프로세스는 모른다고 치자)
    br6 = await broker()
    assert (await br6.lookup_order(_order(6, at=time.time() - 30)))[0] == "unknown"


async def test_overseas_poll_ignores_another_days_order_with_the_same_number(osx):
    """주문번호가 날마다 다시 쓰여도, 다른 날의 같은 번호 주문을 이 주문으로 착각하지 않는다."""
    fake, broker = osx
    br = await broker()
    o = _order(1)
    await br.place_order(o)                                 # 오늘 주문 30100001
    old = datetime.fromtimestamp(time.time() - 2 * 86400, ZoneInfo("America/New_York"))
    fake.extra = [{"odno": "0030100001", "pdno": "NVDA", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "7", "ft_ccld_qty": "3",
                   "ft_ccld_unpr3": "150.00", "nccs_qty": "0", "prcs_stat_name": "완료", "rjct_rson": "",
                   "rvse_cncl_dvsn": "00", "ord_gno_brno": "01790", "ord_dt": old.strftime("%Y%m%d"),
                   "ord_tmd": old.strftime("%H%M%S")}]      # 이틀 전의 같은 번호(3주만 체결되고 끝난 주문)
    assert await br.poll_order(o) == (7, 166.25, True)      # 오늘 것만 본다
    fake.hidden = {1}
    with pytest.raises(BrokerUnknown):
        await br.poll_order(o)                              # 오늘 것이 안 보이면 이틀 전 것으로 대신하지 않는다


def test_overseas_order_row_time_is_treated_as_a_range():
    """주문 행의 접수 시각은 '현지 시각 해석'과 '한국 시각 해석' 사이의 구간으로 다룬다(어느 쪽인지 확인하지 못했다)."""
    cfg = Config.for_market("US")
    cfg.kis.app_key = cfg.kis.app_secret = "x"
    cfg.kis.account = "12345678"
    br = KISOverseasBroker(cfg, "http://127.0.0.1:9", "ws://127.0.0.1:9")
    row = {"ord_dt": "20260105", "ord_tmd": "093500"}
    lo, hi = br._row_span(row)
    assert hi == US.epoch(date(2026, 1, 5), 9, 35) and hi - lo == 14 * 3600        # 겨울: 뉴욕과 서울은 14시간 차이
    assert br._maybe_at(row, hi) and br._maybe_at(row, lo) and not br._maybe_at(row, hi + 3600)
    assert br._maybe_at(row, hi + 200) and not br._maybe_at(row, hi + 400)
    assert br._maybe_at(row, lo - 800, 900) and not br._maybe_at(row, lo - 800)     # 접수가 늦게 찍히는 쪽은 따로 허용폭을 준다
    assert br._row_span({"ord_tmd": "093500"}) is None and br._maybe_at({}, 123.0)   # 날짜가 없으면 판단하지 않는다
    lo2, hi2 = br._row_span({"ord_dt": "20260105"})                                  # 시각이 없으면 그날 전체
    assert hi2 - lo2 > 86000


# ---------------------------------------------------------------------- 알림·시계
async def test_engine_and_executor_risk_events_reach_the_notifier(tmp_path):
    """엔진·집행기가 낸 위험 이벤트(손실 한도, 확인 불가 주문 등)와 심각한 악재 청산은 텔레그램으로도 나간다.
    (이전: 화면에만 표시 — 사람이 보고 있지 않으면 봇이 멈춘 것을 알 길이 없음)"""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    sent = []
    sup.notifier.send = sent.append
    e = sup.engine
    sup._forward_alerts()
    sent.clear()
    e.emit("risk", "", "일일 손실 한도 2.0% 도달 — 오늘 매매 중단")
    e.emit("news", "NVDA", "심각한 악재 확인(Claude): 회계부정 — 보유분 청산", alert=1)
    e.emit("news", "NVDA", "악재 감지: 평범한 소식")                          # 알림 대상 아님
    e.emit("risk", "", "시세 수신 지연 — 신규 매수 보류", sent=1)              # 보낸 곳에서 직접 알린 것
    sup._forward_alerts()
    assert sent == ["⚠ 일일 손실 한도 2.0% 도달 — 오늘 매매 중단", "⚠ [NVDA] 심각한 악재 확인(Claude): 회계부정 — 보유분 청산"]
    sup._forward_alerts()
    assert len(sent) == 2                                   # 같은 이벤트를 두 번 보내지 않는다
    sup.journal.close()


async def test_clock_offset_is_measured_from_server_date_and_reported(tmp_path, unused_tcp_port):
    """서버 응답의 Date 헤더로 이 컴퓨터 시계의 오차를 재고, 크게 어긋나면 경고한다. 주문 접수 시각을 견줄 때도 보정한다."""
    fake = FakeOS2()
    skew = [600.0]                                           # 서버 시계가 10분 빠르다(= 이 컴퓨터가 10분 느리다)
    fake.clock = lambda: time.time() + skew[0]

    @web.middleware
    async def dated(request, handler):
        resp = await handler(request)
        resp.headers["Date"] = formatdate(time.time() + skew[0], usegmt=True)
        return resp

    fake.app.middlewares.append(dated)
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    cfg = os_cfg(tmp_path)
    br = KISOverseasBroker(cfg, f"http://127.0.0.1:{unused_tcp_port}", f"ws://127.0.0.1:{unused_tcp_port}")
    br.rl.interval = 0.0
    try:
        await br.start()
        br.last["NVDA"] = Tick("NVDA", 0.0, 166.19, 1, 166.18, 166.20)
        o = _order(1)
        await br.place_order(o)
        assert abs(br.clock_off - 600.0) < 5.0
        assert await br.poll_order(o) == (7, 166.25, True)   # 10분 어긋나 있어도 제 주문을 찾는다(보정하지 않으면 5분 허용폭 밖)
        sup = Supervisor(cfg, br)
        sup._check_clock()
        sup._check_clock()
        warn = [ev["text"] for ev in sup.engine.events if ev["kind"] == "risk"]
        assert len(warn) == 1 and "느립니다" in warn[0] and "시간 동기화" in warn[0]
        sup.journal.close()
    finally:
        await br.stop()
        await runner.cleanup()


async def test_broker_state_is_saved_and_handed_back_on_restart(tmp_path):
    """증권사 연동이 내보낸 상태(이 봇이 낸 주문번호 등)는 상태 파일에 저장되고, 재시작 때 이전 주문을 정리하기 전에 되돌려진다."""
    class Acct2(Acct):
        got = None

        def export_state(self):
            return {"known": {"30100001": 1.5}}

        def import_state(self, d):
            self.got = d

    cfg = live_cfg(tmp_path)
    br = Acct2(30_000.0)
    sup = Supervisor(cfg, br)
    await sup.reconcile(initial=True)
    sup._save_state()
    sup.journal.close()
    assert json.loads((sup.dir / "state.json").read_text())["broker"] == {"known": {"30100001": 1.5}}
    br2 = Acct2(30_000.0)
    sup2 = Supervisor(cfg, br2)
    await sup2.reconcile(initial=True)
    assert br2.got == {"known": {"30100001": 1.5}}
    sup2.journal.close()


# ---------------------------------------------------------------------- 확인 불가 주문의 자동 재확인·재시작 뒤 추적
class _Flaky(Broker):
    """한동안 조회가 안 되다가(장애) 다시 되는 증권사."""
    supports_poll = True

    def __init__(self):
        super().__init__()
        self.up = False
        self.cum, self.done, self.found = 0, False, ""
        self.cancels = 0

    async def place_order(self, order):
        raise asyncio.TimeoutError()

    async def cancel_order(self, order):
        self.cancels += 1

    async def poll_order(self, order):
        if not self.up:
            raise asyncio.TimeoutError()
        return self.cum, 166.0 if self.cum else 0.0, self.done

    async def lookup_order(self, order):
        if not self.up:
            return "unknown", ""
        return ("found", self.found) if self.found else ("absent", "")


def _exec_env(br):
    e = Engine(Config.for_market("US"), StrategyParams(), 30_000)
    ex = Executor(br, e, 10.0)
    ex.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
    ex.follow_delays = (0.02, 0.05, 0.1)
    ex.stuck_poll_sec = 0.0
    ex.wall = lambda: WALL
    return e, ex


async def test_stuck_order_resolves_itself_once_the_broker_answers_again():
    """확인 불가 주문은 가끔 다시 조회한다. 장애가 끝나 조회가 되면 그 사이의 체결을 장부에 넣고, 끝난 주문이면 종목 잠금을 푼다.
    (정지는 그대로 — 사람이 [재개]를 누른다. 이전: 사람이 누를 때까지 그대로였고, 그 사이의 체결은 장부에 들어오지 않음)"""
    br = _Flaky()
    e, ex = _exec_env(br)
    saved = _saved(WALL - 60)
    saved[0].update(filled=3, avg_price=166.0)              # 이전 실행에서 3주는 이미 장부에 넣었다
    e.on_fill(Fill("NVDA", BUY, 3, 166.0, time.time(), ctx={"stop_dist": 4.0}))
    assert await ex.recover(saved) == 0
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and e.risk.halted and "NVDA" in e.pending
    ex.on_timer(ex.clock())                                 # 아직 장애 중 — 변화 없음
    await asyncio.sleep(0.02)
    assert o.status == "STUCK" and e.positions["NVDA"].qty == 3
    ex.on_fill("1", 2, 166.0, time.time())                  # 그 사이 통보로 2주(누적 5주) — 이전 실행분 다음부터 더한다
    assert e.positions["NVDA"].qty == 5
    br.up, br.cum, br.done = True, 10, True                 # 장애가 끝났다: 전량 체결돼 있었다
    ex.on_timer(ex.clock())
    await asyncio.sleep(0.02)
    assert e.positions["NVDA"].qty == 10 and not ex.orders and "NVDA" not in e.pending
    assert e.risk.halted                                    # 정지는 사람이 푼다
    ex.shutdown()

    # 주문번호조차 모르던 주문(전송 도중 꺼짐): 접수 여부부터 다시 찾는다
    br = _Flaky()
    e, ex = _exec_env(br)
    assert await ex.recover(_saved(WALL - 60, broker_id="", status="SENT")) == 0
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and not o.broker_id
    br.up, br.found, br.cum, br.done = True, "77", 10, True
    ex.on_timer(ex.clock())
    await asyncio.sleep(0.02)
    assert o.broker_id == "77" and o.status == "STUCK"
    ex.on_timer(ex.clock())
    await asyncio.sleep(0.02)
    assert e.positions["NVDA"].qty == 10 and not ex.orders and not e.pending
    ex.shutdown()

    # 증권사에 없다는 답이 두 번 이어지면 접수되지 않은 주문으로 정리한다
    br = _Flaky()
    e, ex = _exec_env(br)
    await ex.recover(_saved(WALL - 60, broker_id="", status="SENT"))
    br.up = True
    for _ in range(2):
        ex.on_timer(ex.clock())
        await asyncio.sleep(0.02)
    assert not ex.orders and not e.pending and "NVDA" not in e.positions
    ex.shutdown()


async def test_recover_keeps_watching_an_order_it_cancelled_without_confirmation():
    """재시작 정리에서 취소한 주문도, 증권사의 '끝남' 표시를 받지 못했으면 얼마간 더 지켜본다.
    (취소 '접수'와 실제 취소 사이에 체결된 수량이 그 뒤에 나타날 수 있다 — 운영 중의 취소와 같은 기준)"""
    br = _Flaky()
    br.up = True                                            # 조회는 되지만 '끝남' 표시는 주지 않는다
    e, ex = _exec_env(br)
    assert await ex.recover(_saved(WALL - 60)) == 0
    assert br.cancels == 1 and not ex.orders and not e.risk.halted and not e.pending
    assert [d["status"] for d in ex.export()] == ["CANCELED"]            # 이 상태로 또 꺼지면 다음에 한 번 더 본다
    br.cum = 4                                              # 취소가 실제로 처리되기 직전에 4주가 체결됐다
    await asyncio.sleep(0.3)
    assert e.positions["NVDA"].qty == 4
    assert ex.export() == []                                # 추적이 끝났다
    ex.on_fill("1", 4, 166.0, time.time())                  # 조회로 이미 넣은 4주의 통보가 뒤늦게 왔다 → 두 번 넣지 않는다
    assert e.positions["NVDA"].qty == 4
    ex.on_fill("1", 1, 166.0, time.time())                  # 그다음 통보(누적 5주)는 새 체결이다
    assert e.positions["NVDA"].qty == 5
    ex.shutdown()


# ====================================================================== 소식으로 거는 청산의 안전장치
def test_news_exit_waits_until_the_price_has_actually_dropped():
    """'심각한 악재' 청산 지시는 가격이 실제로 전일 종가보다 설정한 폭 이상 내려 있을 때만 실행한다.
    (소식 제목은 바깥에서 온 글이다 — 틀렸거나 누가 일부러 흘린 제목 한 줄과 모델의 판정만으로 보유분을 던지지 않는다)
    며칠이 지나도록 가격이 반응하지 않은 지시는 버린다."""
    cfg = Config.for_market("US")
    sent = []
    e = Engine(cfg, StrategyParams(), 30_000, execute=sent.append)
    d1 = date(2026, 1, 5)
    e.on_tick(Tick("X", US.epoch(d1, 10, 0), 100.0, 100))
    e.on_fill(Fill("X", BUY, 10, 100.0, US.epoch(d1, 10, 0), ctx={"stop_dist": 20.0, "atr": 5.0}))
    e.sym["X"].prev_close = 100.0                           # 전일 종가(장전 준비 때 채워진다)
    e.exit_now.add("X")
    e.exit_day["X"] = e.day
    e.on_tick(Tick("X", US.epoch(d1, 10, 1), 99.2, 100))    # −0.8%: 시장은 반응하지 않았다 → 아직 팔지 않는다
    assert not sent and "X" in e.exit_now
    e.on_tick(Tick("X", US.epoch(d1, 10, 2), 98.4, 100))    # −1.6%: 기준(1.5%)을 넘었다 → 청산
    assert [(it.symbol, it.reason, it.limit_price) for it in sent] == [("X", "news_exit", 0)]

    sent.clear()
    e2 = Engine(cfg, StrategyParams(), 30_000, execute=sent.append)
    e2.on_tick(Tick("X", US.epoch(d1, 10, 0), 100.0, 100))
    e2.on_fill(Fill("X", BUY, 10, 100.0, US.epoch(d1, 10, 0), ctx={"stop_dist": 20.0, "atr": 5.0}))
    e2.sym["X"].prev_close = 100.0
    e2.exit_now.add("X")
    e2.exit_day["X"] = e2.day
    assert e2.export_state()["exit_day"] == {"X": e2.day}
    for k in (1, 2, 3):                                     # 사흘 뒤까지는 지시가 살아 있다(주말을 넘겨도)
        e2.on_tick(Tick("X", US.epoch(d1 + timedelta(days=k), 10, 0), 100.5, 100))
        assert "X" in e2.exit_now and not sent
    e2.on_tick(Tick("X", US.epoch(d1 + timedelta(days=7), 10, 0), 100.5, 100))
    assert "X" not in e2.exit_now and not e2.exit_day and not sent          # 닷새 넘게 가격이 반응하지 않았다 → 버린다

    cfg0 = Config.for_market("US")
    cfg0.news.exit_min_drop_pct = 0.0                       # 조건을 끄면 예전처럼 즉시
    e3 = Engine(cfg0, StrategyParams(), 30_000, execute=sent.append)
    e3.on_tick(Tick("X", US.epoch(d1, 10, 0), 100.0, 100))
    e3.on_fill(Fill("X", BUY, 10, 100.0, US.epoch(d1, 10, 0), ctx={"stop_dist": 20.0, "atr": 5.0}))
    e3.sym["X"].prev_close = 100.0
    e3.exit_now.add("X")
    e3.on_tick(Tick("X", US.epoch(d1, 10, 1), 100.4, 100))
    assert sent and sent[-1].reason == "news_exit"


async def test_news_exit_orders_are_capped_per_day():
    """하루에 소식으로 거는 청산 지시 수에는 상한이 있다 — 넘으면 알리기만 하고 팔지 않는다(제목 여러 개로 보유분 전체를 던지게 하는 일 방지)."""
    held = ("AMD", "NVDA", "AAPL", "MSFT")
    llm = SymLLM(default=-2)
    cfg, e, na = news_env2(llm, held=held)
    cfg.news.max_exits_per_day = 2
    na.ingest([(NOW, f"{name} hit by accounting fraud probe", "", "x", ()) for name in ("AMD", "Nvidia", "Apple", "Microsoft")], NOW)
    assert await na.classify(NOW + 400) == 4
    assert len(e.exit_now) == 2 and set(e.exit_day) == e.exit_now
    over = [ev for ev in e.events if ev.get("alert") and "한도" in ev["text"]]
    assert len(over) == 2 and all(e.news[s].block_until > NOW for s in held)      # 나머지는 알림 + 매수 보류만
    na.new_day()                                            # 날이 바뀌면 상한은 새로 센다
    assert na.exits_day == 0
