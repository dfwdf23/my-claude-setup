"""느린 경로·통합 검증: 총괄 일과, 대시보드, 주문 집행, Claude 계층(가짜 클라이언트), 복기."""
import asyncio
import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import aiohttp
import pytest

from aitrader.agents.learner import Learner
from aitrader.agents.market import MarketAnalyst
from aitrader.backtest import Backtester
from aitrader.brokers.base import Broker, BrokerError
from aitrader.brokers.sim import PaperBroker, SimMarket
from aitrader.config import Config
from aitrader.dashboard.server import Dashboard
from aitrader.engine import Engine
from aitrader.executor import Executor
from aitrader.journal import Journal
from aitrader.llm import LLM
from aitrader.market import KRX
from aitrader.models import BUY, Bar, OrderIntent, Tick
from aitrader.params import ParamStore, StrategyParams
from aitrader.supervisor import Supervisor


T0 = KRX.epoch(date(2026, 1, 5), 10, 0)        # 월요일 10:00 KST (장중)


def sim_cfg(tmp_path, days=2) -> Config:
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    cfg.sim.speed = 1e9
    cfg.sim.tick_sec = 10.0
    cfg.sim.days = days
    cfg.sim.latency_ms = 0.0
    cfg.llm.enabled = False
    return cfg


async def test_supervisor_runs_full_days_unattended(tmp_path):
    cfg = sim_cfg(tmp_path, days=3)
    sup = Supervisor(cfg, PaperBroker(cfg))
    await asyncio.wait_for(sup.run(), 120)
    e = sup.engine
    assert len(sup.days) == 3 and sup.phase == "CLOSED"
    assert len(e.trades) > 0 and not e.positions                     # 매매가 일어났고 당일 청산됨
    assert abs(e.equity - (cfg.capital + e.realized_total)) < 1.0    # 회계 항등식
    assert len(sup.learner.reports) == 3                              # 매일 장후 복기 수행
    con = sqlite3.connect(tmp_path / "sim" / "journal.db")
    assert con.execute("select count(*) from bars").fetchone()[0] > 500
    assert con.execute("select count(*) from trades").fetchone()[0] == len(e.trades)
    assert con.execute("select count(*) from daily").fetchone()[0] == 3
    assert con.execute("select count(*) from lessons").fetchone()[0] >= 3
    causes = [json.loads(r[0]) for r in con.execute("select causes from trades where pnl < 0")]
    assert causes and all(c for c in causes)                          # 손실 거래마다 원인 태그가 저장됨
    snap = sup.snapshot()
    assert {a["role"] for a in snap["agents"]} >= {"총괄", "시장분석", "차트분석", "비중조절", "매수", "매도",
                                                   "과매수 방지", "리스크 관리", "주문 집행", "복기·학습", "종목선정"}
    assert snap["latency"]["decision_us"]["n"] == e.ticks


async def test_dashboard_api_and_controls(tmp_path, unused_tcp_port):
    cfg = sim_cfg(tmp_path, days=0)
    cfg.sim.speed = 2000
    sup = Supervisor(cfg, PaperBroker(cfg))
    dash = Dashboard(sup, "127.0.0.1", unused_tcp_port, token="s3cret")
    await dash.start()
    task = asyncio.create_task(sup.run())
    base = f"http://127.0.0.1:{unused_tcp_port}"
    try:
        await asyncio.sleep(0.5)
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                assert r.status == 200 and "AI 자동매매 총괄" in await r.text()
            async with s.get(base + "/api/state") as r:
                assert r.status == 401                                # 토큰 없으면 거부
            async with s.post(base + "/api/control", json={"action": "halt"}) as r:
                assert r.status == 401
            h = {"X-Token": "s3cret"}
            async with s.get(base + "/api/state", headers=h) as r:
                st = await r.json()
                assert st["mode"] == "sim" and st["phase"] in ("PRE", "OPEN") and not st["halted"]
            async with s.post(base + "/api/control", json={"action": "flatten"}, headers=h) as r:
                assert r.status == 200
            assert sup.engine.risk.halted and sup.engine.risk.flatten_all
            async with s.post(base + "/api/control", json={"action": "nope"}, headers=h) as r:
                assert r.status == 400
            async with s.post(base + "/api/control", json={"action": "resume"}, headers=h) as r:
                assert r.status == 200
            assert not sup.engine.risk.halted
            async with s.ws_connect(base + "/ws?token=s3cret") as ws:
                msg = await ws.receive_json(timeout=3)
                assert "equity" in msg and "agents" in msg
    finally:
        sup.stop()
        await asyncio.wait_for(task, 10)
        await dash.stop()


def test_dashboard_refuses_public_bind_without_token():
    with pytest.raises(ValueError):
        Dashboard(None, "0.0.0.0", 8787, token="")


class FakeBroker(Broker):
    """주문을 접수만 하고, 체결은 테스트가 직접 흘려 넣는다."""
    supports_poll = True

    def __init__(self):
        super().__init__()
        self.n = 0
        self.reject = False
        self.poll_result = (0, 0.0, False)
        self.canceled = []

    async def place_order(self, order):
        if self.reject:
            raise BrokerError("증거금 부족")
        self.n += 1
        order.broker_id = str(self.n)

    async def cancel_order(self, order):
        self.canceled.append(order.broker_id)

    async def poll_order(self, order):
        return self.poll_result


def _exec_env():
    cfg = Config()
    cfg.etfs = ["E"]
    br = FakeBroker()
    e = Engine(cfg, StrategyParams(), 10_000_000)
    ex = Executor(br, e, timeout_sec=10.0)
    e.on_tick(Tick("E", T0, 10_000, 1, 10_000, 10_005))
    return br, e, ex


async def test_executor_fill_dedupe_between_notice_and_poll():
    br, e, ex = _exec_env()
    it = OrderIntent("E", BUY, 100, 10_005, "entry", {"stop_dist": 200, "atr": 100})
    e.pending["E"] = it
    ex.submit(it)
    await asyncio.sleep(0.01)
    o = next(iter(ex.orders.values()))
    br.poll_result = (40, 10_005.0, False)
    await ex._poll(o)                                   # 폴링이 먼저 40주 체결을 발견
    assert e.positions["E"].qty == 40
    br.on_fill("1", 40, 10_005.0, e.now)                # 같은 40주에 대한 통보가 뒤늦게 도착 → 이중 반영 금지
    assert e.positions["E"].qty == 40
    br.on_fill("1", 60, 10_005.0, e.now)                # 나머지 60주
    assert e.positions["E"].qty == 100 and not ex.orders and "E" not in e.pending


async def test_executor_early_fill_reject_and_timeout_cancel():
    br, e, ex = _exec_env()
    it = OrderIntent("E", BUY, 10, 10_005, "entry", {"stop_dist": 200, "atr": 100})
    e.pending["E"] = it
    br.on_fill("1", 10, 10_005.0, e.now)                # 주문 응답보다 체결통보가 먼저 온 경우
    ex.submit(it)
    await asyncio.sleep(0.01)
    assert e.positions["E"].qty == 10 and not ex.orders
    # 거부: 잠금이 풀리고 재시도 지연이 걸린다
    br.reject = True
    it2 = OrderIntent("E", BUY, 10, 10_005, "entry")
    e.pending["E"] = it2
    ex.submit(it2)
    await asyncio.sleep(0.01)
    assert "E" not in e.pending and e.sym["E"].retry_after > e.now and len(e.risk.reject_ts) == 1
    # 시간 초과 → 취소, 취소 직후 도착한 늦은 체결도 장부에 반영
    br.reject = False
    it3 = OrderIntent("E", BUY, 5, 9_000, "entry", {"stop_dist": 200, "atr": 100})
    e.pending["E"] = it3
    ex.submit(it3)
    await asyncio.sleep(0.01)
    ex.can_poll = False
    ex.on_timer(e.now + 11)
    await asyncio.sleep(0.01)
    assert br.canceled == ["2"] and not ex.orders and "E" not in e.pending
    before = e.positions["E"].qty
    br.on_fill("2", 5, 9_000.0, e.now)
    assert e.positions["E"].qty == before + 5


class FakeMessages:
    def __init__(self, text, fail=None):
        self.text, self.fail, self.calls = text, fail, []

    async def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                               content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=self.text)],
                               usage=SimpleNamespace(input_tokens=700, output_tokens=300,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


def fake_llm(text, budget=60_000, fail=None) -> tuple[LLM, FakeMessages]:
    cfg = Config().llm
    cfg.daily_token_budget = budget
    llm = LLM(cfg)
    fm = FakeMessages(text, fail)
    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=fm), messages=fm)
    llm.enabled = True
    return llm, fm


async def test_llm_cache_budget_and_request_shape():
    llm, fm = fake_llm('{"a": 1}', budget=1500)
    schema = {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"], "additionalProperties": False}
    out = await llm.ask_json("market", "claude-opus-5-5", "low", "sys", {"x": 1}, schema)
    assert out == {"a": 1} and llm.tokens_today == 1000 and llm.calls_today == 1
    kw = fm.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["fallbacks"] == "default"
    assert "thinking" not in kw and "temperature" not in kw
    assert kw["system"][0]["cache_control"]["type"] == "ephemeral"
    assert await llm.ask_json("market", "claude-opus-5-5", "low", "sys", {"x": 1}, schema) == {"a": 1}
    assert len(fm.calls) == 1 and llm.cache_hits == 1                 # 같은 입력은 다시 묻지 않는다
    await llm.ask_json("market", "claude-opus-5-5", "low", "sys", {"x": 2}, schema)
    assert len(fm.calls) == 2 and llm.tokens_today == 2000
    assert await llm.ask_json("market", "claude-opus-5-5", "low", "sys", {"x": 3}, schema) is None
    assert len(fm.calls) == 2 and llm.skipped_budget == 1             # 예산 초과 → 호출 차단
    llm.new_day("2099-01-01")
    assert llm.tokens_today == 0


async def test_llm_failure_degrades_to_none():
    llm, fm = fake_llm("not json")
    assert await llm.ask_json("r", "claude-haiku-5-5", "low", "s", {}, {}) is None      # JSON 아님
    assert "fallbacks" not in fm.calls[0]                             # 하이쿠는 서버 측 대체 모델 미지원
    llm2, _ = fake_llm("{}", fail=RuntimeError("boom"))
    assert await llm2.ask_json("r", "claude-opus-5-5", "low", "s", {}, {}) is None
    assert not llm2.enabled                                           # 알 수 없는 오류 → 비활성화하고 규칙 기반으로


def _ready_engine(cfg) -> Engine:
    mk = SimMarket(cfg.universe, seed=5, tick_sec=10.0, etfs=frozenset(cfg.etfs))
    bt = Backtester(cfg, StrategyParams())
    bt.run_ticks(mk.day_ticks())
    return bt.engine


async def test_market_analyst_llm_is_gated_and_bounded():
    cfg = Config()
    e = _ready_engine(cfg)
    llm, fm = fake_llm('{"bias":"risk_off","risk_mult":7,"block_entries":false,"avoid":["069500","999999"],"reason":"테스트"}')
    ma = MarketAnalyst(cfg, llm)
    br = Broker()
    reg = await ma.update(e, br, force=True)
    assert len(fm.calls) == 1 and reg.source == "llm"
    assert reg.risk_mult <= 1.0                                       # 배수는 0~1로 제한(7 → 1 이하)
    assert reg.avoid == frozenset({"069500"})                         # 모르는 종목코드는 버린다
    assert e.regime is reg
    await ma.update(e, br)                                            # 상황이 그대로면 다시 묻지 않는다
    e.now += 3600
    await ma.update(e, br)
    assert len(fm.calls) == 1
    payload = json.loads(fm.calls[0]["messages"][0]["content"])
    assert set(payload) <= {"m", "s", "idx", "news"} and len(fm.calls[0]["messages"][0]["content"]) < 1500


async def test_market_rule_regime_without_llm():
    cfg = Config()
    cfg.llm.enabled = False
    e = _ready_engine(cfg)
    ma = MarketAnalyst(cfg, LLM(cfg.llm))
    reg = await ma.update(e, Broker())
    assert reg.source == "rule" and 0.0 <= reg.risk_mult <= 1.0
    crash = ma.rule_regime({"n": 8, "breadth": 0.1, "above_vwap": 0.1, "avg_chg": -3.0, "vol_x": 1.0}, 0.0)
    assert crash.bias == "risk_off" and "069500" in crash.avoid and "114800" not in crash.avoid   # 인버스는 허용


def _recorded_days(cfg, n=4):
    """n일치 합성 시세를 돌려 '기록된 봉'과 '거래 내역'을 만든다."""
    mk = SimMarket(cfg.universe, seed=11, tick_sec=10.0, etfs=frozenset(cfg.etfs))
    bt = Backtester(cfg, StrategyParams())
    bars: list[Bar] = []
    bt.engine.on_bar_cb = bars.append
    for _ in range(n):
        bt.run_ticks(mk.day_ticks())
        bt.engine.flush_bars()
        mk.next_day()
    return bars, bt.engine.trades


async def test_learner_review_validates_before_adopting(tmp_path):
    cfg = Config()
    bars, trades = _recorded_days(cfg)
    store = ParamStore(tmp_path / "p.json")
    proposal = {"lessons": ["테스트 교훈"], "proposals": [
        {"name": "터무니없는 값", "why": "범위 밖", "changes": [{"param": "rsi_overbought", "value": 5},
                                                         {"param": "max_daily_loss", "value": 0.9}]}]}
    llm, fm = fake_llm(json.dumps(proposal, ensure_ascii=False))
    ln = Learner(cfg, store, llm, Journal(None))
    rep = await ln.review("2026-01-08", trades[-10:], trades, bars, {}, 0.0)
    assert len(fm.calls) == 1 and rep["source"] == "llm" and "테스트 교훈" in rep["lessons"]
    assert rep["tested"] and rep["tested"][0]["name"] == "현재값"     # 후보는 반드시 과거 기록으로 검증
    names = [t["name"] for t in rep["tested"]]
    assert any(n.startswith("Claude:") for n in names) and any(n.startswith("규칙:") for n in names)
    p = store.current
    assert p.rsi_overbought >= 69.0                                   # 1회 변경폭(3) 이상은 못 움직인다
    if rep["adopted"]:
        assert p.version == 2 and any(t.get("ok") for t in rep["tested"])
    else:
        assert p.version == 1 and not any(t.get("ok") for t in rep["tested"])
    payload = fm.calls[0]["messages"][0]["content"]
    assert len(payload) < 6000                                        # 복기 입력도 압축 요약(원시 시세를 보내지 않음)
    # 채택 직후에는 효과 측정 기간 동안 추가 변경을 하지 않는다
    ln.days_since_change = 0
    rep2 = await ln.review("2026-01-09", trades[-10:], trades, bars, {}, 0.0)
    assert not rep2["tested"] and len(fm.calls) == 1


async def test_learner_needs_enough_samples(tmp_path):
    cfg = Config()
    bars, trades = _recorded_days(cfg, n=4)
    store = ParamStore(None)
    ln = Learner(cfg, store, LLM(SimpleNamespace(enabled=False)), Journal(None))
    rep = await ln.review("d", trades[:3], trades[:3], bars, {}, 0.0)
    # 실거래가 3건뿐이어도, 기록된 봉을 현재 값으로 재생해 얻은 거래를 표본으로 삼아 후보를 만들고 검증한다
    assert "과거 기록 재생" in rep.get("sample", "") and rep["tested"] and rep["tested"][0]["name"] == "현재값"
    store = ParamStore(None)
    ln = Learner(cfg, store, LLM(SimpleNamespace(enabled=False)), Journal(None))
    # 기록이 3거래일 미만이면 표본외 검증을 할 수 없으므로 아무것도 바꾸지 않는다
    two_days = [b for b in bars if cfg.mkt.local_day(b.ts) <= cfg.mkt.local_day(bars[0].ts) + 1]
    rep2 = await ln.review("d", trades, trades, two_days, {}, 0.0)
    assert store.current.version == 1 and not rep2["tested"]
    assert any("표본외 검증" in x for x in rep2["lessons"])


def test_learner_throttle_and_rollback():
    cfg = Config()
    store = ParamStore(None)
    ln = Learner(cfg, store, LLM(SimpleNamespace(enabled=False)), Journal(None))
    ln.day_pnls = [-1, -1, -1]
    assert "축소" in ln._throttle() and store.current.size_mult == 0.75 and store.current.version == 1
    ln.day_pnls.append(5)
    assert "복원" in ln._throttle() and store.current.size_mult == 1.0
