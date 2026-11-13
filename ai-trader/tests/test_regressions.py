"""독립 검토에서 확인된 결함의 회귀 테스트. 각 테스트는 '고치기 전에는 실패하던' 상황을 재현한다."""
import asyncio
import json
import time
from datetime import date

import pytest

from aitrader.backtest import Backtester
from aitrader.brokers.base import Broker, BrokerError
from aitrader.brokers.sim import PaperBroker
from aitrader.config import Config, load_dotenv
from aitrader.engine import Engine
from aitrader.executor import Executor
from aitrader.market import KRX
from aitrader.models import BUY, SELL, AccountSnapshot, Bar, Fill, OrderIntent, Tick
from aitrader.params import StrategyParams
from aitrader.supervisor import Supervisor

T0 = KRX.epoch(date(2026, 1, 5), 10, 0)


class Book(Broker):
    """증권사 흉내: 주문을 받아 장부에 적고, 테스트가 원하는 방식으로 실패·체결시킨다."""
    supports_poll = True

    def __init__(self):
        super().__init__()
        self.n = 0
        self.orders: dict[str, dict] = {}
        self.fail_after_accept = False      # 접수는 됐는데 응답이 유실되는 상황
        self.fail_before_accept = False     # 전송 자체가 실패(접수 안 됨)
        self.lookup_mode = "real"           # real | unknown
        self.cash = 10_000_000.0
        self.holdings: dict[str, int] = {}
        self.account_delay = 0.0
        self.canceled: list[str] = []

    async def place_order(self, order):
        if self.fail_before_accept:
            raise asyncio.TimeoutError()
        self.n += 1
        bid = str(self.n)
        self.orders[bid] = {"symbol": order.symbol, "side": order.side, "qty": order.qty, "filled": 0,
                            "price": order.limit_price or 10_000.0, "done": False}
        if self.fail_after_accept:
            raise asyncio.TimeoutError()
        order.broker_id = bid

    def fill(self, bid: str, qty: int | None = None, notify: bool = True):
        o = self.orders[bid]
        q = qty if qty is not None else o["qty"] - o["filled"]
        o["filled"] += q
        o["done"] = o["filled"] >= o["qty"]
        d = q if o["side"] == BUY else -q
        self.holdings[o["symbol"]] = self.holdings.get(o["symbol"], 0) + d
        if notify:
            self.on_fill(bid, q, o["price"], T0)

    async def cancel_order(self, order):
        o = self.orders.get(order.broker_id)
        if o is None or o["done"]:
            raise BrokerError("이미 체결")
        o["done"] = True
        self.canceled.append(order.broker_id)

    async def poll_order(self, order):
        o = self.orders[order.broker_id]
        return o["filled"], o["price"], o["done"]

    async def lookup_order(self, order):
        if self.lookup_mode == "unknown":
            return "unknown", ""
        for bid, o in self.orders.items():
            if o["symbol"] == order.symbol and o["side"] == order.side and o["qty"] == order.qty and not o.get("claimed"):
                o["claimed"] = True
                return "found", bid
        return "absent", ""

    async def get_account(self):
        snap = AccountSnapshot(self.cash, {s: (q, 10_000.0) for s, q in self.holdings.items() if q > 0})
        if self.account_delay:
            await asyncio.sleep(self.account_delay)
        return snap


def env(cfg=None):
    cfg = cfg or Config()
    cfg.etfs = ["E", "F"]
    br = Book()
    e = Engine(cfg, StrategyParams(), 10_000_000)
    ex = Executor(br, e, timeout_sec=10.0)
    ex.resolve_delays = (0.01, 0.01, 0.01, 0.01)
    for sym in ("E", "F"):
        e.on_tick(Tick(sym, T0, 10_000, 1, 10_000, 10_005))
    return br, e, ex


def buy(e, ex, sym="E", qty=100):
    it = OrderIntent(sym, BUY, qty, 10_005, "entry", {"stop_dist": 200, "atr": 100})
    e.pending[sym] = it
    ex.submit(it)
    return it


async def test_fill_polling_actually_runs_when_notice_is_missed():
    """체결통보가 누락돼도 조회(폴링)로 체결을 찾아낸다. (이전: 폴링 조건이 영원히 거짓이라 주문이 영구 정지)"""
    br, e, ex = env()
    buy(e, ex)
    await asyncio.sleep(0.01)
    o = next(iter(ex.orders.values()))
    assert o.id in ex.polled                             # 접수 시점에 재확인 시각이 잡혀야 한다
    br.fill("1", notify=False)                           # 증권사에서는 체결됐지만 통보가 오지 않음
    ex.on_timer(T0 + 1)
    await asyncio.sleep(0.01)
    assert "E" not in e.positions                        # 아직 1.5초가 안 지났다
    ex.polled[o.id] -= 1.6                               # 1.6초 경과
    ex.on_timer(T0 + 2)
    await asyncio.sleep(0.01)
    assert e.positions["E"].qty == 100 and not ex.orders and "E" not in e.pending


async def test_lost_response_order_is_resolved_not_rejected():
    """주문 응답이 유실돼도(접수는 됨) 잠금을 풀지 않고 증권사에 물어 주문을 찾아 붙인다."""
    br, e, ex = env()
    br.fail_after_accept = True
    buy(e, ex)
    await asyncio.sleep(0.005)
    o = next(iter(ex.orders.values()))
    assert o.status == "UNKNOWN" and "E" in e.pending    # 확인 전에는 종목 잠금 유지(재주문 방지)
    await asyncio.sleep(0.05)
    assert o.status == "ACK" and o.broker_id == "1"
    br.fill("1")
    assert e.positions["E"].qty == 100 and br.holdings["E"] == 100 and "E" not in e.pending
    assert not e.risk.reject_ts                          # 거부로 세지 않는다


async def test_lost_response_not_accepted_releases_lock():
    br, e, ex = env()
    br.fail_before_accept = True
    buy(e, ex)
    await asyncio.sleep(0.08)
    assert not ex.orders and "E" not in e.pending and "E" not in e.positions


async def test_unresolvable_order_keeps_lock_and_halts_until_human_resumes():
    br, e, ex = env()
    br.fail_after_accept = True
    br.lookup_mode = "unknown"
    buy(e, ex)
    await asyncio.sleep(0.1)
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and "E" in e.pending and e.risk.halted and "확인" in e.risk.halt_reason
    assert ex.drop_stuck() == ["E"] and "E" not in e.pending


async def test_fill_callback_exception_does_not_leak_lock():
    """일지·상태 저장 훅이 예외를 던져도 주문은 정상 종료되고 잠금이 풀린다."""
    br, e, ex = env()
    e.on_fill_cb = lambda fl: 1 / 0
    buy(e, ex)
    await asyncio.sleep(0.01)
    br.fill("1")
    assert e.positions["E"].qty == 100 and not ex.orders and "E" not in e.pending and e.hook_errors == 1


async def test_recover_orders_left_by_previous_run():
    """꺼져 있는 동안 체결된 수량은 장부에 반영하고, 남은 주문은 취소한다."""
    br, e, ex = env()
    br.orders["77"] = {"symbol": "E", "side": BUY, "qty": 100, "filled": 60, "price": 10_000.0, "done": False}
    saved = [{"id": 5, "symbol": "E", "side": BUY, "qty": 100, "limit_price": 10_005, "reason": "entry",
              "ctx": {"stop_dist": 200, "atr": 100}, "created_ts": T0, "status": "ACK", "broker_id": "77",
              "broker_org": "", "filled": 0}]
    assert await ex.recover(saved) == 1
    assert e.positions["E"].qty == 60 and br.canceled == ["77"]


def test_pending_buys_reserve_cash_and_exposure():
    """체결 전인 매수 주문도 한도 계산에 포함한다(동시 신호로 총 비중 한도를 넘지 않게)."""
    cfg = Config()
    cfg.etfs = ["E", "F"]
    sent = []
    e = Engine(cfg, StrategyParams(), 10_000_000, execute=sent.append)
    for sym in ("E", "F"):
        e.on_tick(Tick(sym, T0, 10_000, 1, 10_000, 10_005))
    it = OrderIntent("E", BUY, 700, 10_005, "entry")
    e._send(it, e.sym["E"])
    assert e.reserved == 700 * 10_005
    # 두 번째 신호의 수량 계산: 예약분을 뺀 현금·더한 비중으로 → 80% 한도까지 남은 만큼만
    qty, _ = e.sizer.size(10_000, 5_000, 1.0, e.equity, e.cash - e.reserved, e.exposure + e.reserved,
                          1.0, e.p, etf=True)
    assert (e.reserved + qty * 10_000) <= e.equity * cfg.risk.max_exposure
    e.on_order_done("E", False, "미체결")
    assert e.reserved == 0.0 and not e._resv


async def test_simultaneous_signals_never_exceed_exposure_cap(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    cfg.sim.speed, cfg.sim.tick_sec, cfg.sim.days, cfg.sim.latency_ms = 1e9, 10.0, 2, 500.0
    cfg.llm.enabled = False
    cfg.risk.max_positions, cfg.risk.max_weight, cfg.risk.max_exposure = 8, 0.45, 0.80
    sup = Supervisor(cfg, PaperBroker(cfg))
    e = sup.engine
    worst = [0.0, 0.0]
    orig = sup._on_fill

    def watch(fl):
        orig(fl)
        e.mark()
        worst[0] = max(worst[0], e.exposure / e.equity)
        worst[1] = min(worst[1], e.cash)
    e.on_fill_cb = watch
    await asyncio.wait_for(sup.run(), 120)
    assert len(e.trades) > 0
    assert worst[0] <= cfg.risk.max_exposure + 0.03 and worst[1] >= 0.0


def test_bar_replay_fills_at_trigger_level_not_bar_extreme():
    """봉 재생: 익절은 익절가에, 손절은 손절가에 체결(봉의 고가·저가가 아니라). 갭이면 시가."""
    cfg = Config.for_market("US")
    cfg.fees.slippage_ticks = 0
    t = cfg.mkt.epoch(date(2026, 1, 5), 10, 0)

    def run(bar):
        bt = Backtester(cfg, StrategyParams())
        e = bt.engine
        e.on_tick(Tick("X", t, 100.0, 1))
        e.on_fill(Fill("X", BUY, 10, 100.0, t, ctx={"stop_dist": 2.0, "atr": 1.0}))   # 손절 98, 익절 104
        bt.run_bars([bar])
        return e.trades[-1]

    tp = run(Bar("X", t + 180, 100.5, 106.0, 100.2, 105.0, 400))
    assert tp.exit_reason == "take_profit" and tp.exit_price == 104.0          # 106(고가)이 아니라 104
    sl = run(Bar("X", t + 180, 99.5, 100.0, 95.0, 96.0, 400))
    assert sl.exit_reason == "stop_loss" and sl.exit_price == 98.0             # 95(저가)가 아니라 98
    gap = run(Bar("X", t + 180, 97.0, 97.5, 96.0, 96.5, 400))
    assert gap.exit_reason == "stop_loss" and gap.exit_price == 97.0           # 갭 하락: 시가에 체결
    both = run(Bar("X", t + 180, 100.0, 105.0, 97.0, 104.5, 400))              # 한 봉에 손절·익절 모두
    assert both.exit_reason == "stop_loss"                                     # 불리한 쪽(손절 먼저)


def live_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.mode = "kis-mock"
    cfg.data_dir = str(tmp_path)
    cfg.etfs = ["E", "F"]
    cfg.llm.enabled = False
    cfg.news.enabled = False
    cfg.capital = 10_000_000
    return cfg


def live_sup(tmp_path, br=None):
    br = br or Book()
    sup = Supervisor(live_cfg(tmp_path), br)
    e = sup.engine
    for sym in ("E", "F"):
        e.on_tick(Tick(sym, T0, 10_000, 1, 10_000, 10_005))
    return br, sup, e


async def test_reconcile_ignores_snapshot_taken_before_a_fill(tmp_path):
    """잔고 조회가 진행되는 사이 체결된 매수를 '계좌에 없다'며 지우지 않는다."""
    br, sup, e = live_sup(tmp_path)
    br.account_delay = 0.05
    task = asyncio.create_task(sup.reconcile())
    await asyncio.sleep(0.01)                            # 잔고 스냅샷(0주)은 이미 떠졌고 응답 대기 중
    buy(e, sup.executor, "E", 200)
    await asyncio.sleep(0.005)
    br.fill("1")
    assert e.positions["E"].qty == 200
    await task
    assert e.positions["E"].qty == 200 and not e.risk.halted
    sup.journal.close()


async def test_reconcile_needs_two_mismatches_and_books_a_sale(tmp_path):
    """장부를 줄여야 할 때: 두 번 연속 확인된 뒤에만, '매도'로 기록해 현금을 맞춘다(자산이 증발하지 않게)."""
    br, sup, e = live_sup(tmp_path)
    buy(e, sup.executor, "E", 200)
    await asyncio.sleep(0.005)
    br.fill("1")
    eq0 = e.equity
    br.holdings["E"] = 0                                 # 누군가 수동으로 전량 매도
    br.cash += 200 * 10_000
    await sup.reconcile()
    assert e.positions["E"].qty == 200 and not e.risk.halted          # 한 번의 불일치로는 움직이지 않는다
    await sup.reconcile()
    assert "E" not in e.positions and e.risk.halted and not e.risk.flatten_all
    assert abs(e.equity - eq0) < eq0 * 0.001                           # 자산이 포지션만큼 사라지지 않는다
    assert not e.risk.day_stop and e.trades[-1].exit_reason == "reconcile"
    sup.journal.close()


async def test_first_start_with_small_account_does_not_trip_drawdown(tmp_path):
    """계좌 현금이 설정 자본보다 훨씬 적어도 시작하자마자 '낙폭 정지'가 걸리지 않는다."""
    br = Book()
    br.cash = 5_000_000.0                                # 설정 자본(1,000만)의 50%
    sup = Supervisor(live_cfg(tmp_path), br)
    await sup.reconcile(initial=True)
    e = sup.engine
    e.on_tick(Tick("E", T0, 10_000, 1, 10_000, 10_005))
    e.mark()
    assert e.cash == 5_000_000 and e.risk.peak_equity == 5_000_000 and not e.risk.halted and e.risk.drawdown == 0
    sup.journal.close()


async def test_restart_keeps_ledger_and_daily_loss_state(tmp_path):
    """같은 날 재시작해도 실현손익·당일 손실 한도 상태가 이어진다(한도가 처음부터 다시 시작되지 않는다)."""
    br, sup, e = live_sup(tmp_path)
    await sup.reconcile(initial=True)
    buy(e, sup.executor, "E", 200)
    await asyncio.sleep(0.005)
    br.fill("1")
    e.on_fill(Fill("E", SELL, 100, 9_000, T0 + 60, reason="stop_loss"))       # 일부 손절 → 실현손실
    br.holdings["E"] = 100
    e.day = KRX.local_day(time.time())                   # '오늘' 거래 중이었다고 가정
    e.risk.day_stop = True
    e.risk.trades_today = 7
    sup._save_state()
    cash, realized = e.cash, e.realized_total
    assert realized < 0
    sup.journal.close()

    br2 = Book()
    br2.cash, br2.holdings = 50_000_000.0, {"E": 100}    # 계좌에는 봇 예산보다 훨씬 많은 현금이 있다
    sup2 = Supervisor(live_cfg(tmp_path), br2)
    await sup2.reconcile(initial=True)
    e2 = sup2.engine
    assert e2.cash == cash and e2.realized_total == realized          # 예산이 '설정 자본'으로 되돌아가지 않는다
    assert e2.positions["E"].qty == 100 and e2.day == e.day
    assert e2.risk.day_stop and e2.risk.trades_today == 7             # 당일 한도 상태 유지
    st = json.loads((tmp_path / "kis-mock" / "state.json").read_text())
    assert st["engine"]["risk"]["day_stop"] is True
    sup2.journal.close()


async def test_drawdown_halt_can_be_resumed(tmp_path):
    """낙폭 정지 후 [재개]를 누르면 기준점을 현재 자산으로 다시 잡는다(이전: 누르자마자 다시 정지)."""
    br, sup, e = live_sup(tmp_path)
    e.on_fill(Fill("E", BUY, 500, 10_000, T0, ctx={"stop_dist": 9_000, "atr": 100}))
    e.on_tick(Tick("E", T0 + 10, 8_000, 1, 8_000, 8_005))           # -10% 평가손 → 낙폭 정지
    assert e.risk.halted and "낙폭" in e.risk.halt_reason
    sup.control("resume")
    e.on_tick(Tick("E", T0 + 20, 8_000, 1, 8_000, 8_005))
    e.mark()
    assert not e.risk.halted and not e.risk.day_stop and e.risk.drawdown == 0
    sup.journal.close()


def test_dotenv_ignores_inline_comments_and_empty_values(tmp_path, monkeypatch):
    """.env 의 'KEY=   # 설명' 줄이 '# 설명'이라는 값으로 읽히지 않는다."""
    p = tmp_path / ".env"
    p.write_text("KIS_ACCOUNT=            # 계좌번호 앞 8자리\nKIS_PRODUCT=01          # 뒤 2자리\nX_REAL=abc\n", encoding="utf-8")
    for k in ("KIS_ACCOUNT", "KIS_PRODUCT", "X_REAL"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(str(p))
    import os
    assert "KIS_ACCOUNT" not in os.environ and os.environ["KIS_PRODUCT"] == "01" and os.environ["X_REAL"] == "abc"


def test_config_rejects_wrong_types_and_unknown_keys():
    from aitrader.config import _merge
    cfg = Config()
    with pytest.raises(ValueError):
        _merge(cfg, {"risk": {"max_positions": "many"}})
    with pytest.raises(ValueError):
        _merge(cfg, {"session": {"intraday_only": 1}})
    with pytest.raises(ValueError):
        _merge(cfg, {"nope": 1})
    _merge(cfg, {"capital": 1000, "risk": {"max_positions": 3.0}})
    assert cfg.capital == 1000.0 and cfg.risk.max_positions == 3
