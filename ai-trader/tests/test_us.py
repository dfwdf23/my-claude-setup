"""미국 시장·스윙 방식·정보수집 검증."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

from aitrader.agents.news import NewsAgent, item_id, parse_feed, rule_score, safe_url
from aitrader.backtest import Backtester
from aitrader.brokers.base import Broker
from aitrader.brokers.sim import PaperBroker
from aitrader.config import Config, load_config
from aitrader.engine import Engine
from aitrader.llm import LLM
from aitrader.market import KRX, US
from aitrader.models import BUY, Bar, Fill, Tick
from aitrader.params import ParamStore, StrategyParams
from aitrader.research import load_yahoo, run_research
from aitrader.supervisor import Supervisor


def test_us_tick_money_and_dst():
    assert US.tick(187.3) == 0.01 and US.tick(0.5) == 0.0001
    assert US.round_tick(187.333, True) == 187.34 and US.round_tick(187.339, False) == 187.33
    assert US.round_tick(187.33, True) == 187.33                      # 이미 호가 단위면 그대로(부동소수 오차 없음)
    assert KRX.round_tick(30_010, True) == 30_050 and KRX.tick(30_000, etf=True) == 5
    assert US.money(-1234.5) == "-$1,234.50" and KRX.money(1234.4) == "+1,234원"
    winter = US.epoch(date(2026, 1, 5), 9, 30)
    summer = US.epoch(date(2026, 7, 6), 9, 30)
    assert US.utc_offset(winter) == -5 * 3600 and US.utc_offset(summer) == -4 * 3600   # 서머타임
    assert US.local_day(winter) == (date(2026, 1, 5) - date(1970, 1, 1)).days


def test_engine_uses_exchange_local_session_across_dst():
    """미국 장 시간은 현지 시각 기준 — 서머타임 여부와 무관하게 09:30~16:00만 처리한다."""
    cfg = Config.for_market("US", "intraday")
    for d in (date(2026, 1, 5), date(2026, 7, 6)):
        e = Engine(cfg, StrategyParams(), 30_000)
        e.on_tick(Tick("SPY", US.epoch(d, 9, 0), 500.0, 10))           # 장 시작 전
        assert "SPY" not in e.sym and e.now == 0
        e.on_tick(Tick("SPY", US.epoch(d, 9, 30), 500.0, 10))
        e.on_tick(Tick("SPY", US.epoch(d, 15, 59), 501.0, 10))
        e.on_tick(Tick("SPY", US.epoch(d, 16, 0), 999.0, 10))          # 마감 이후
        assert e.sym["SPY"].last == 501.0 and e.sym["SPY"].day_open == 500.0
        assert e.day == US.local_day(US.epoch(d, 12, 0))


def test_default_config_is_us_swing_and_krx_still_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.market == "US" and cfg.style == "swing" and cfg.strategy.bar_sec == 86_400
    assert not cfg.session.intraday_only and "SQQQ" not in cfg.universe          # 인버스는 장기 보유 대상에서 제외
    assert ParamStore(None, cfg.params).current.take_profit_r == 5.0
    assert cfg.learn.enabled and not cfg.learn.auto_apply                         # 복기는 하되 자동 적용은 기본으로 끔
    (tmp_path / "config.toml").write_text('market = "KRX"\n[risk]\nmax_positions = 3\n', encoding="utf-8")
    k = load_config()
    assert k.market == "KRX" and k.risk.max_positions == 3 and k.universe[0] == "069500"
    (tmp_path / "config.toml").write_text('style = "intraday"\n', encoding="utf-8")
    i = load_config()
    assert i.market == "US" and i.style == "intraday" and i.session.intraday_only and i.strategy.bar_sec == 180


def test_relative_volume_and_daily_context():
    cfg = Config.for_market("US", "intraday")
    e = Engine(cfg, StrategyParams(), 30_000)
    day0 = US.epoch(date(2026, 1, 5))
    daily = [Bar("X", day0 - (60 - i) * 86400, 100 + i, 101 + i, 99 + i, 100 + i, 1_000_000) for i in range(60)]
    e.set_daily("X", daily)
    s = e.sym["X"]
    assert s.prev_close == 159 and s.prev_high == 160 and s.avg_dvol == 1_000_000 and s.dtrend == 1.0   # 정배열
    # 장 중간(50%)까지 평소의 2배가 거래됐다면 상대 거래량은 약 2
    t0 = US.epoch(date(2026, 1, 5), 9, 30)
    half = t0 + 3.25 * 3600
    e.on_tick(Tick("X", half - 180, 160.0, 0, cum_volume=999_000))
    e.on_tick(Tick("X", half, 160.0, 0, cum_volume=1_000_000))
    assert 1.9 < s.rvol < 2.1
    # 상대 거래량이 하한보다 낮으면 차트분석이 신호를 내지 않는다
    from aitrader.models import Features
    f = Features(close=105, ema_fast=104, ema_slow=102, rsi=60, atr=1, bb_upper=108, bb_mid=103, vwap=103,
                 hh=104.5, vol_ratio=2.0, ready=True)
    p = StrategyParams()
    assert e.chart.evaluate("X", f, p, rvol=1.5) is not None
    assert e.chart.evaluate("X", f, p, rvol=p.rvol_min - 0.1) is None
    assert e.chart.evaluate("X", f, p, rvol=-1.0) is not None           # 일봉 자료가 없으면 거르지 않는다


def test_swing_decides_at_next_open_without_lookahead():
    """일봉 방식: 그날 봉이 닫힌 뒤 '다음 날' 장 초반에 판단하고 그때 가격에 산다(당일 종가를 미리 쓰지 않는다)."""
    cfg = Config.for_market("US")
    cfg.session.entry_start = cfg.session.open
    cfg.fees.slippage_ticks = 0
    sent = []
    e = Engine(cfg, ParamStore(None, cfg.params).current, 30_000, execute=sent.append)
    px = 100.0
    d = date(2026, 1, 5)
    day_n = 0
    steps = (1.016, 1.012, 0.990, 1.014, 0.994)          # 눌림이 섞인 상승 추세(과매수로만 치닫지 않게)
    while len(sent) == 0 and day_n < 80:                 # 예열 후 언젠가 매수 신호가 난다
        t = US.epoch(d, 9, 30)
        nxt = px * steps[day_n % len(steps)]
        for i, p_ in enumerate((px, min(px, nxt) * 0.996, max(px, nxt) * 1.004, nxt)):
            before = len(sent)
            e.on_tick(Tick("UP", t + i * 450, round(p_, 2), 10_000))
            if len(sent) > before:
                assert i == 0                             # 주문은 그날의 첫 틱(시가)에서만 나온다
                assert sent[-1].side == BUY and abs(sent[-1].limit_price - round(px, 2)) < 0.5
        e.flush_bars()                                    # 장 마감: 일봉을 닫는다(판단은 다음 날)
        px = nxt
        d = date.fromordinal(d.toordinal() + (3 if d.weekday() == 4 else 1))
        day_n += 1
    assert sent, f"상승 추세에서 매수 신호가 한 번은 나와야 한다 (차단: {dict(e.guard.blocks)} {dict(e.risk.blocks)})"


async def test_us_swing_supervisor_runs_unattended(tmp_path):
    cfg = Config.for_market("US")
    cfg.data_dir = str(tmp_path)
    cfg.sim.speed, cfg.sim.tick_sec, cfg.sim.days, cfg.sim.latency_ms = 1e9, 60.0, 45, 0.0
    cfg.llm.enabled = False
    sup = Supervisor(cfg, PaperBroker(cfg))
    await asyncio.wait_for(sup.run(), 180)
    e = sup.engine
    assert len(sup.days) == 45 and (tmp_path / "sim-us" / "journal.db").exists()
    assert e.fill_seq > 0                                              # 일봉 방식에서도 매매가 일어난다
    held = sum(p.qty * e.sym[p.symbol].last for p in e.positions.values())
    assert abs(e.equity - (e.cash + held)) < 0.01
    assert abs((e.cash + sum(p.cost for p in e.positions.values())) - (cfg.capital + e.realized_total)) < 0.5
    snap = sup.snapshot()
    assert snap["market"]["currency"] == "USD" and snap["market"]["tz_off"] in (-18000, -14400)
    ch = sup.chart(sup.symbols[0])
    assert ch["bars"] and len(ch["bars"][0]) == 9 and ch["view"]       # 전략 봉(일봉) + 화면용 분봉


def _yahoo(path, rows):
    q = {k: [r[i] for r in rows] for i, k in enumerate(("open", "high", "low", "close", "volume"), start=1)}
    path.write_text(json.dumps({"chart": {"result": [{"timestamp": [int(r[0]) for r in rows],
                                                      "indicators": {"quote": [q]}}]}}))


def test_research_runs_on_yahoo_format_daily_bars(tmp_path):
    rows, px, d = [], 100.0, date(2025, 1, 6)
    for i in range(160):
        o = px
        px *= 1.004 if (i // 20) % 2 == 0 else 0.998
        rows.append((US.epoch(d, 9, 30), o, max(o, px) * 1.004, min(o, px) * 0.996, px, 1_000_000))
        d = date.fromordinal(d.toordinal() + (3 if d.weekday() == 4 else 1))
    _yahoo(tmp_path / "AAA_1d_5y.json", rows)
    _yahoo(tmp_path / "AAA_1d_1y.json", rows)
    assert len(load_yahoo(tmp_path / "AAA_1d_5y.json", "AAA")) == 160
    cfg = Config.for_market("US")
    cfg.universe = ["AAA"]
    out = run_research(cfg, str(tmp_path), "1d_5y", quiet=True)
    assert out["days"] == 160 and out["bar_sec"] == 86_400 and out["result"]["n"] + out["open"] > 0
    assert "AAA" in out["hold"] and abs(out["ret"]) < 1.0


# ------------------------------------------------------------------ 정보수집
RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Acme cuts full-year guidance after weak demand</title><link>https://example.com/a</link>
<pubDate>Fri, 13 Nov 2026 12:02:19 +0000</pubDate><guid isPermaLink="false">1</guid></item>
<item><title>Acme beats estimates, raises outlook - Example Wire</title><link>javascript:alert(1)</link>
<pubDate>Fri, 13 Nov 2026 11:00:00 +0000</pubDate></item></channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - Acme Corp</title>
<link href="https://example.com/8k"/><updated>2026-11-13T12:05:00Z</updated></entry></feed>"""


def test_feed_parsing_and_rule_scoring():
    items = parse_feed(RSS, "x")
    assert len(items) == 2 and items[0][1].startswith("Acme cuts") and items[0][0] == 1794571339.0
    assert parse_feed(ATOM, "x")[0] == (1794571500.0, "8-K - Acme Corp", "https://example.com/8k")
    assert parse_feed(b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", "x") == []      # 엔티티 선언이 있는 문서는 거부
    assert parse_feed(b"not xml", "x") == []
    assert safe_url("javascript:alert(1)") == "" and safe_url("https://a.b/c") == "https://a.b/c"
    assert rule_score("Acme cuts full-year guidance after weak demand")[0] <= -0.7
    assert rule_score("Acme beats estimates, raises outlook")[0] >= 0.7
    assert rule_score("Acme announces $2B public offering of common stock")[0] <= -0.8
    assert rule_score("Acme to present at investor conference")[0] == 0.0
    assert item_id("Acme beats estimates - Example Wire") == item_id("ACME beats   estimates")   # 매체명 꼬리·대소문자 무시


class FakeLLM:
    def __init__(self, out):
        self.enabled, self.out, self.calls = True, out, []

    async def ask_json(self, role, model, effort, system, payload, schema, max_tokens=0):
        self.calls.append(payload)
        return self.out


def news_env(llm=None):
    cfg = Config.for_market("US")
    cfg.universe = ["ACME", "NVDA"]
    cfg.news.aliases = {"ACME": ["Acme"], "NVDA": ["Nvidia"]}
    e = Engine(cfg, StrategyParams(), 30_000)
    na = NewsAgent(cfg, llm or LLM(SimpleNamespace(enabled=False)), e, Broker())
    na.set_symbols(cfg.universe)
    return cfg, e, na


def test_news_blocks_buys_on_bad_news_and_dedupes():
    cfg, e, na = news_env()
    now = 1794571400.0
    rows = [(ts, title, link, "x", ()) for ts, title, link in parse_feed(RSS, "x")]
    assert na.ingest(rows, now) == 2 and na.ingest(rows, now) == 0            # 같은 소식은 한 번만
    ns = e.news["ACME"]
    assert ns.block_until == now + cfg.news.block_min * 60                    # 악재 → 일정 시간 매수 보류
    assert na.ingest([(now, "Nvidia wins approval for new chip", "", "x", ())], now) == 1
    assert e.news["NVDA"].score > 0 and e.news["NVDA"].block_until == 0
    assert na.ingest([(now, "Unrelated company does something", "", "x", ())], now) == 0   # 감시 종목과 무관
    old = now - cfg.news.max_age_min * 60 - 10
    na.ingest([(old, "Acme faces lawsuit over old product", "", "x", ())], now + 1)
    assert not next(reversed(na.items.values())).fresh                        # 오래된 소식은 판단에 쓰지 않는다
    view = na.recent(10)
    assert [v["ts"] for v in view] == sorted((v["ts"] for v in view), reverse=True)   # 화면에는 최신 소식이 위로
    assert view[-1]["title"].startswith("Acme faces") and not view[-1]["fresh"]
    assert all(v["url"] == "" or v["url"].startswith("http") for v in view)


async def test_news_claude_classification_unblocks_and_forces_exit():
    out = {"items": [{"i": 0, "impact": 0, "kind": "analyst"}, {"i": 1, "impact": -2, "kind": "legal"}]}
    llm = FakeLLM(out)
    cfg, e, na = news_env(llm)
    now = 1794571400.0
    na.ingest([(now, "Acme downgraded by one analyst on valuation", "", "x", ())], now)       # 규칙: 악재
    na.ingest([(now, "Nvidia under investigation for accounting fraud", "", "x", ())], now)
    assert e.news["ACME"].block_until > now and e.news["NVDA"].block_until > now
    assert await na.classify(now + 400) == 2 and len(llm.calls) == 1
    assert llm.calls[0]["items"][0][:2] == [0, "ACME"] and len(json.dumps(llm.calls[0])) < 600   # 제목만 압축해서 보낸다
    assert e.news["ACME"].block_until == 0.0                                  # Claude가 '영향 없음' → 차단 해제
    assert "NVDA" in e.exit_now and e.news["NVDA"].block_until > now          # 심각한 악재 → 보유분 청산 지시
    assert await na.classify(now + 500) == 0 and len(llm.calls) == 1          # 최소 간격 안에는 다시 묻지 않는다


def test_engine_respects_news_block_and_news_exit():
    cfg = Config.for_market("US", "intraday")
    cfg.fees.slippage_ticks = 0
    sent = []
    e = Engine(cfg, StrategyParams(), 30_000, execute=sent.append)
    t = US.epoch(date(2026, 1, 5), 10, 0)
    e.on_tick(Tick("X", t, 100.0, 100))
    e.on_fill(Fill("X", BUY, 10, 100.0, t, ctx={"stop_dist": 2.0, "atr": 1.0}))
    e.exit_now.add("X")
    e.on_tick(Tick("X", t + 5, 100.5, 100))
    assert sent and sent[-1].reason == "news_exit" and sent[-1].limit_price == 0


def test_daily_bar_is_seeded_when_started_mid_session():
    """일봉 방식에서 장중에 켜면 오늘 봉의 지나간 구간(시가·고가·저가·거래량)을 채운 뒤 틱으로 이어 간다."""
    cfg = Config.for_market("US")
    d = date(2026, 1, 5)
    now = US.epoch(d, 11, 0)
    part = Bar("SPY", US.epoch(d, 9, 30), 500.0, 505.0, 495.0, 501.0, 1_000_000)
    e = Engine(cfg, StrategyParams(), 30_000)
    e.seed_bar("SPY", part, US.epoch(d, 8, 0))                         # 장 시작 전에는 아무것도 하지 않는다
    assert "SPY" not in e.sym or e.sym["SPY"].bar_start < 0
    e.seed_bar("SPY", part, now)
    s = e.sym["SPY"]
    assert (s.o, s.h, s.l, s.c, s.v) == (500.0, 505.0, 495.0, 501.0, 1_000_000)
    assert s.day_open == 500.0 and s.cum_vol == 1_000_000 and s.last == 501.0
    e.on_tick(Tick("SPY", now + 20, 502.0, 0, 0.0, 0.0, 1_000_000))    # 첫 조회(증가분 0)
    e.on_tick(Tick("SPY", now + 40, 506.0, 700, 0.0, 0.0, 1_000_700))
    e.seed_bar("SPY", Bar("SPY", part.ts, 1.0, 2.0, 0.5, 1.5, 9), now + 60)   # 진행 중인 봉은 덮어쓰지 않는다
    e.flush_bars()
    assert s.hist[-1][1:6] == (500.0, 506.0, 495.0, 506.0, 1_000_700)  # 하루 전체 범위·거래량이 담긴 일봉
    assert s.hist[-1][0] == US.epoch(d, 9, 30)                         # 일봉 시각 = 그날 장 시작(현지 날짜가 밀리지 않는다)
    assert s.day_open == 500.0


class DailyStub(Broker):
    """고정된 시각과 일봉(오늘의 미완성 봉 포함)을 돌려주는 브로커."""

    def __init__(self, now, bars):
        super().__init__()
        self._now, self._bars = now, bars

    def now(self):
        return self._now

    async def get_daily_bars(self, symbol, n):
        return [Bar(symbol, *b) for b in self._bars][-n:]


async def test_supervisor_mid_session_start_uses_complete_daily_bars(tmp_path):
    cfg = Config.for_market("US")
    cfg.data_dir, cfg.universe, cfg.llm.enabled, cfg.news.enabled = str(tmp_path), ["SPY"], False, False
    d = date(2026, 3, 13)                                              # 금요일
    days = []
    x = d
    while len(days) < 80:
        x = date.fromordinal(x.toordinal() - 1)
        if x.weekday() < 5:
            days.append(x)
    hist = [(US.epoch(x, 9, 30), 100.0 + i * 0.1, 101.0 + i * 0.1, 99.0 + i * 0.1, 100.5 + i * 0.1, 1_000)
            for i, x in enumerate(reversed(days))]
    partial = (US.epoch(d, 9, 30), 108.0, 109.0, 107.0, 108.5, 400)   # 오늘 11시까지의 미완성 일봉
    now = US.epoch(d, 11, 0)
    br = DailyStub(now, hist + [partial])
    sup = Supervisor(cfg, br)
    await sup.day_start(d)
    e, s = sup.engine, sup.engine.sym["SPY"]
    assert s.prev_close == hist[-1][4] and s.avg_dvol == 1_000         # 일봉 맥락은 '어제까지'만(오늘 미완성 봉 제외)
    assert (s.o, s.h, s.l, s.v) == (108.0, 109.0, 107.0, 400)          # 오늘 봉은 지나간 구간부터 이어 간다
    assert s.hist[-1][0] == hist[-1][0]                                # 예열에도 오늘 미완성 봉은 넣지 않는다
    e.on_tick(Tick("SPY", now + 30, 110.0, 250, 0.0, 0.0, 650))
    e.on_tick(Tick("SPY", US.epoch(d, 15, 59), 109.5, 350, 0.0, 0.0, 1_000))
    e.flush_bars()
    sup.journal.flush()
    bars = sup._recent_bars(US.epoch(d, 16, 10))
    today = [b for b in bars if US.local(b.ts).date() == d]
    assert len(today) == 1                                             # 미완성 봉과 마감 봉이 겹쳐 들어가지 않는다
    assert (today[0].open, today[0].high, today[0].low, today[0].close, today[0].volume) == (108.0, 110.0, 107.0, 109.5, 1_000)
    sup.journal.close()
