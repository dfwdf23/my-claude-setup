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
