"""두 번째 독립 검토에서 재현된 결함의 회귀 테스트 — 주문 집행과 증권사 연동.

각 테스트는 '고치기 전에는 장부와 계좌가 어긋나던' 상황을 그대로 재현한다.
"""
import asyncio
from pathlib import Path
from datetime import date

import pytest
from aiohttp import web

from aitrader.brokers.base import Broker, BrokerError, BrokerUnknown
from aitrader.brokers.kis_overseas import MAX_PAGES, KISOverseasBroker
from aitrader.config import Config
from aitrader.engine import Engine
from aitrader.executor import Executor
from aitrader.market import KRX, US
from aitrader.models import BUY, SELL, Fill, Order, OrderIntent, Tick
from aitrader.params import StrategyParams

from test_kis_overseas import FakeOS, os_cfg

T0 = KRX.epoch(date(2026, 1, 5), 10, 0)


class Desk(Broker):
    """증권사 흉내. 체결은 테스트가 직접 일으키고, 통보(건별)와 조회(누적 수량·평균가)를 따로 흘린다."""
    supports_poll = True

    def __init__(self):
        super().__init__()
        self.n = 0
        self.book: dict[str, dict] = {}
        self.cancel_effective = True        # 취소 요청이 곧바로 처리되는가(False: 접수만 되고 주문은 아직 살아 있다)
        self.poll_fail = 0                  # 앞으로 이만큼의 조회가 실패한다
        self.polls = 0

    async def place_order(self, order):
        self.n += 1
        order.broker_id = str(self.n)
        self.book[order.broker_id] = {"side": order.side, "qty": order.qty, "fills": [], "done": False}

    def fill(self, bid: str, qty: int, px: float, notify: bool = False):
        o = self.book[bid]
        o["fills"].append((qty, px))
        if sum(q for q, _ in o["fills"]) >= o["qty"]:
            o["done"] = True
        if notify:
            self.on_fill(bid, qty, px, T0)

    async def cancel_order(self, order):
        o = self.book[order.broker_id]
        if o["done"]:
            raise BrokerError("취소할 수량이 없습니다")
        if self.cancel_effective:
            o["done"] = True

    async def poll_order(self, order):
        self.polls += 1
        if self.poll_fail > 0:
            self.poll_fail -= 1
            raise asyncio.TimeoutError()
        o = self.book[order.broker_id]
        cum = sum(q for q, _ in o["fills"])
        avg = sum(q * p for q, p in o["fills"]) / cum if cum else 0.0
        return cum, avg, o["done"]


def desk_env():
    cfg = Config()
    cfg.etfs = ["E"]
    br = Desk()
    e = Engine(cfg, StrategyParams(), 10_000_000)
    ex = Executor(br, e, timeout_sec=10.0)
    ex.settle_delays = (0.0, 0.01, 0.01, 0.01)
    ex.follow_delays = (0.03, 0.03)
    e.on_tick(Tick("E", T0, 10_000, 1, 10_000, 10_005))
    return br, e, ex


def send(e, ex, side=BUY, qty=100):
    it = OrderIntent("E", side, qty, 10_005 if side == BUY else 0.0, "entry" if side == BUY else "stop_loss",
                     {"stop_dist": 200, "atr": 100})
    e.pending["E"] = it
    ex.submit(it)


async def timeout_cancel(ex):
    ex.on_timer(T0 + 11)                                 # 10초 제한을 넘겼다 → 취소 절차 시작
    await asyncio.sleep(0.08)


async def test_late_notice_after_cancel_is_not_booked_twice():
    """취소 직후 조회로 이미 반영한 체결의 통보가 뒤늦게 와도 또 넣지 않는다. (이전: 40주가 80주로 기록)"""
    br, e, ex = desk_env()
    send(e, ex)
    await asyncio.sleep(0.01)
    br.fill("1", 40, 10_000)                             # 취소가 날아가는 사이 40주 체결(통보는 아직)
    await timeout_cancel(ex)
    assert e.positions["E"].qty == 40 and not ex.orders and "E" not in e.pending
    br.on_fill("1", 40, 10_000, T0)                      # 같은 체결의 통보가 이제야 도착
    assert e.positions["E"].qty == 40
    br.on_fill("1", 10, 10_010, T0)                      # 이것은 정말로 새 체결(취소 처리 직전에 체결된 10주)
    assert e.positions["E"].qty == 50
    br.on_fill("1", 10, 10_010, T0)
    assert e.positions["E"].qty == 60 and abs(e.positions["E"].avg_price - (40 * 10_000 + 20 * 10_010) / 60) < 1e-6


async def test_fill_before_cancel_is_kept_when_the_first_polls_fail():
    """취소 직후의 조회가 실패해도 그 사이 체결분을 버리지 않는다. (이전: 조회 1회 실패 → 계좌에만 있는 주식)"""
    br, e, ex = desk_env()
    send(e, ex)
    await asyncio.sleep(0.01)
    br.fill("1", 60, 10_000)
    br.poll_fail = 2
    await timeout_cancel(ex)
    assert e.positions["E"].qty == 60 and not ex.orders and not e.risk.halted


async def test_unconfirmable_cancel_keeps_lock_and_calls_a_human():
    """최종 체결 수량을 끝내 확인할 수 없으면 '취소됨'으로 단정하지 않고 잠금을 유지한 채 정지한다."""
    br, e, ex = desk_env()
    send(e, ex)
    await asyncio.sleep(0.01)
    br.fill("1", 60, 10_000)
    br.poll_fail = 99
    await timeout_cancel(ex)
    o = next(iter(ex.orders.values()))
    assert o.status == "STUCK" and "E" in e.pending and e.risk.halted and "체결 수량" in e.risk.halt_reason
    assert not e.risk.flatten_all                        # 사람이 계좌를 확인할 일이지 전량 청산할 일이 아니다


async def test_fill_that_lands_after_cancel_was_accepted_is_found_later():
    """취소가 '접수'만 되고 처리되기 전에 체결된 주문 — 취소로 끝낸 뒤에도 추적 조회로 찾아 장부에 넣는다."""
    br, e, ex = desk_env()
    br.cancel_effective = False
    ex.follow_delays = (0.15, 0.15)
    send(e, ex)
    await asyncio.sleep(0.01)
    await timeout_cancel(ex)
    assert not ex.orders and "E" not in e.positions      # 두 번 연속 '체결 0'을 확인하고 취소로 끝냈다
    br.fill("1", 100, 10_005)                            # 그런데 취소보다 체결이 먼저 처리됐다
    await asyncio.sleep(0.25)
    assert e.positions["E"].qty == 100
    assert any("취소 처리 뒤" in ev["text"] for ev in e.events)
    ex.shutdown()


async def test_shutdown_does_not_turn_an_unconfirmed_cancel_into_a_halt():
    """종료 중에 취소 확정이 끝나지 않았다고 '확인 불가 → 정지'를 저장하지 않는다. 그 주문은 다음 시작 때 정리한다."""
    br, e, ex = desk_env()
    ex.settle_delays = (0.0, 0.2, 0.2, 0.2)
    br.cancel_effective = False
    send(e, ex)
    await asyncio.sleep(0.01)
    br.poll_fail = 99                                    # 연결이 닫혀 조회가 계속 실패하는 상황
    await ex.cancel_all(timeout=0.05)
    ex.shutdown()
    await asyncio.sleep(0.5)
    assert not e.risk.halted
    saved = ex.export()
    assert len(saved) == 1 and saved[0]["status"] == "CANCELING" and saved[0]["broker_id"] == "1"
    br.poll_fail = 0                                     # 다음 시작: 조회가 되고, 그 사이 30주가 체결돼 있었다
    br.fill("1", 30, 10_000)
    e2 = Engine(Config(), StrategyParams(), 10_000_000)
    ex2 = Executor(br, e2)
    ex2.settle_delays = (0.0, 0.01, 0.01, 0.01)
    e2.on_tick(Tick("E", T0, 10_000, 1, 10_000, 10_005))
    assert await ex2.recover(saved) == 1 and e2.positions["E"].qty == 30


async def test_polled_partial_fills_are_booked_at_their_own_prices():
    """조회가 주는 가격은 누적 평균이다 — 늘어난 수량의 가격을 역산해 총 체결금액을 맞춘다.
    (이전: 두 번째 50주를 누적 평균 99.6에 기록 → 매도 대금이 실제보다 많게 잡힘)"""
    br, e, ex = desk_env()
    e.on_fill(Fill("E", BUY, 100, 10_000.0, T0, ctx={"stop_dist": 200, "atr": 100}))
    cash0 = e.cash
    send(e, ex, SELL, 100)
    await asyncio.sleep(0.01)
    o = next(iter(ex.orders.values()))
    br.fill("1", 50, 10_000.0)
    await ex._poll(o)
    br.fill("1", 50, 9_920.0)                            # 누적 평균은 9,960
    await ex._poll(o)
    assert not e.positions and not ex.orders
    proceeds = (50 * 10_000.0 + 50 * 9_920.0) * (1 - e.commission - e.sym["E"].tax)
    assert abs(e.cash - cash0 - proceeds) < 0.5
    fills = [ev for ev in e.events if ev["kind"] == "fill" and ev.get("side") == "SELL"]
    assert [round(f["price"]) for f in fills] == [10_000, 9_920]


async def test_recover_books_only_the_new_part_at_its_own_price():
    """재시작 정리: 저장된 주문의 '이미 반영한 수량·평균가'를 기준으로 늘어난 만큼만, 제 가격에 넣는다."""
    br, e, ex = desk_env()
    br.book["9"] = {"side": BUY, "qty": 100, "fills": [(40, 10_000.0), (20, 10_060.0)], "done": False}
    e.on_fill(Fill("E", BUY, 40, 10_000.0, T0, ctx={"stop_dist": 200, "atr": 100}))      # 꺼지기 전에 반영된 40주
    saved = [{"id": 3, "symbol": "E", "side": BUY, "qty": 100, "limit_price": 10_060, "reason": "entry",
              "ctx": {"stop_dist": 200, "atr": 100}, "created_ts": T0, "status": "ACK", "broker_id": "9",
              "broker_org": "", "filled": 40, "avg_price": 10_000.0}]
    assert await ex.recover(saved) == 1
    pos = e.positions["E"]
    assert pos.qty == 60 and abs(pos.avg_price - (40 * 10_000 + 20 * 10_060) / 60) < 1e-6
    assert br.book["9"]["done"]                          # 남은 수량은 취소


# ---------------------------------------------------------------------- 한국투자증권 해외주식 연동
class FakeOS2(FakeOS):
    """주문체결내역을 실제 접수된 주문으로 만들어 주고, 쪽 나눔(연속 조회)과 게이트웨이 오류를 흉내 낸다."""
    PAGE = 15

    def __init__(self, env="mock"):
        super().__init__(env)
        self.gateway_error = False          # 주문은 접수되지만 응답이 게이트웨이 오류 화면
        self.business_reject_tr = ""        # 이 TR로 온 매도는 평범한 사유(잔고 부족)로 거부
        self.invalid_tr = ""                # 이 TR은 '없는 TR'
        self.earlier = 0                    # 오늘 주문 앞에 쌓여 있는 다른 주문 수
        self.ccnl_calls = 0

    async def order(self, req):
        tr = req.headers["tr_id"]
        if tr == self.invalid_tr:
            return web.json_response({"rt_cd": "1", "msg_cd": "EGW00206", "msg1": "TR 코드가 유효하지 않습니다"})
        if tr == self.business_reject_tr:
            self.business_reject_tr = ""                                # 한 번만
            return web.json_response({"rt_cd": "1", "msg_cd": "APBK0400", "msg1": "주문가능수량을 초과했습니다"})
        r = await super().order(req)
        if self.gateway_error:
            return web.Response(status=504, text="<html><body>504 Gateway Time-out</body></html>",
                                content_type="text/html")
        return r

    async def ccnl(self, req):
        self.ccnl_calls += 1
        rows = [{"odno": f"00200{i:05d}", "pdno": "MSFT", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "1", "ft_ccld_qty": "1",
                 "ft_ccld_unpr3": "500", "nccs_qty": "0", "prcs_stat_name": "완료", "rjct_rson": "",
                 "rvse_cncl_dvsn": "00", "ord_gno_brno": "01790"} for i in range(self.earlier)]
        for i, (tr, b) in enumerate(self.orders, 1):
            rows.append({"odno": f"00301{i:05d}", "pdno": b["PDNO"], "sll_buy_dvsn_cd": "02" if tr.endswith("1002U") else "01",
                         "ft_ord_qty": b["ORD_QTY"], "ft_ccld_qty": b["ORD_QTY"], "ft_ccld_unpr3": b["OVRS_ORD_UNPR"],
                         "nccs_qty": "0", "prcs_stat_name": "완료", "rjct_rson": "", "rvse_cncl_dvsn": "00",
                         "ord_gno_brno": "01790"})
        start = int(req.query.get("CTX_AREA_NK200") or 0)
        page = rows[start:start + self.PAGE]
        more = start + self.PAGE < len(rows)
        return web.json_response({"rt_cd": "0", "output": page, "ctx_area_fk200": "x",
                                  "ctx_area_nk200": str(start + self.PAGE)}, headers={"tr_cont": "M" if more else "D"})


@pytest.fixture
async def os2(tmp_path, unused_tcp_port):
    fake = FakeOS2()
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    cfg = os_cfg(tmp_path)
    br = KISOverseasBroker(cfg, f"http://127.0.0.1:{unused_tcp_port}", f"ws://127.0.0.1:{unused_tcp_port}")
    br.rl.interval = 0.0                                 # 테스트에서는 호출 간격 제한을 끈다
    await br.start()
    br.last["NVDA"] = Tick("NVDA", 0.0, 166.19, 1, 166.18, 166.20)
    yield fake, br, cfg
    await br.stop()
    await runner.cleanup()


async def test_gateway_error_after_accept_is_unknown_not_rejected(os2):
    """주문은 접수됐는데 응답이 게이트웨이 오류 화면이면 '거부'가 아니다 — 조회로 찾아 붙인다.
    (이전: 거부로 처리 → 계좌에는 주문이 살아 있는데 봇은 모름)"""
    fake, br, cfg = os2
    fake.gateway_error = True
    with pytest.raises(BrokerUnknown):
        await br.place_order(Order(1, "NVDA", BUY, 10, 166.25, "entry", {}, 0.0))
    assert len(fake.orders) == 1                         # 증권사에는 주문이 접수돼 있다

    e = Engine(cfg, StrategyParams(), 30_000)
    ex = Executor(br, e, timeout_sec=10.0)
    ex.resolve_delays = (0.01, 0.01, 0.01)
    e.on_tick(Tick("NVDA", US.epoch(date(2026, 1, 5), 10, 0), 166.19, 1, 166.18, 166.20))
    it = OrderIntent("NVDA", BUY, 7, 166.25, "entry", {"stop_dist": 4.0, "atr": 2.0})
    e.pending["NVDA"] = it
    ex.submit(it)
    await asyncio.sleep(0.3)
    assert len(fake.orders) == 2 and not e.risk.reject_ts               # 거부로 세지 않았고, 다시 주문하지도 않았다
    o = next(iter(ex.orders.values()))
    assert o.status == "ACK" and o.broker_id == "30100002" and "NVDA" in e.pending   # 조회로 주문을 찾아 붙였다
    await ex._poll(o)                                                   # 이후 체결은 평소처럼 조회로 확인
    assert e.positions["NVDA"].qty == 7 and not ex.orders


async def test_mock_sell_tr_is_not_pinned_by_an_ordinary_rejection(os2):
    """모의 매도 TR 탐색: 평범한 거부(수량 초과 등)를 'TR이 틀렸다'로 단정해 다른 TR에 고정하지 않는다.
    (이전: 첫 매도가 업무 사유로 거부되면 틀린 TR에 고정돼 이후 매도가 전부 실패)"""
    fake, br, cfg = os2
    fake.business_reject_tr = "VTTT1001U"                # 문서상의 TR이 맞는 TR이고, 첫 매도는 수량 초과로 거부된다
    fake.invalid_tr = "VTTT1006U"
    with pytest.raises(BrokerError) as err:
        await br.place_order(Order(1, "NVDA", SELL, 999, 166.0, "stop_loss", {}, 0.0))
    assert "주문가능수량" in str(err.value)               # 사람이 봐야 할 것은 처음의 진짜 사유
    assert br._sell_tr == ""                             # 아직 어느 쪽도 확정하지 않았다
    o = Order(2, "NVDA", SELL, 5, 166.0, "stop_loss", {}, 0.0)
    await br.place_order(o)
    assert o.broker_id and br._sell_tr == "VTTT1001U" and fake.orders[-1][0] == "VTTT1001U"


async def test_mock_order_history_is_read_to_the_end(os2):
    """모의투자 주문체결내역은 오래된 순·전 종목으로만 온다 — 끝까지 읽어야 방금 낸 주문이 보인다.
    (이전: 6쪽에서 멈춰, 앞선 주문이 90건을 넘으면 체결을 영영 못 봄)"""
    fake, br, cfg = os2
    fake.earlier = 95
    o = Order(1, "NVDA", BUY, 10, 166.25, "entry", {}, 0.0)
    await br.place_order(o)
    assert await br.poll_order(o) == (10, 166.25, True)
    assert fake.ccnl_calls == 7                          # 96건 ÷ 15건
    fake.earlier = MAX_PAGES * FakeOS2.PAGE + 5          # 한 번에 다 읽을 수 없을 만큼 많으면
    with pytest.raises(BrokerUnknown):
        await br.poll_order(o)                           # '없음'이 아니라 '모름'
    assert (await br.lookup_order(Order(2, "NVDA", BUY, 3, 1.0, "entry", {}, 0.0)))[0] == "unknown"


# ====================================================================== 재시작·일과·보안
# (같은 검토에서 재현된 결함: 재시작 직후 멀쩡한 계좌를 '큰 손실'로 착각해 전량 청산이 나가던 문제 등)
import json
import time
from datetime import timedelta

import aiohttp

from aitrader.agents.news import parse_feed
from aitrader.brokers.manual import ManualBroker
from aitrader.dashboard.server import Dashboard
from aitrader.models import AccountSnapshot, Bar
from aitrader.supervisor import Supervisor, store_dir

SYMS = [f"S{i}" for i in range(9)]


class Acct(Broker):
    """계좌가 있는 증권사 흉내(체결하면 계좌 잔고·현금도 함께 바뀐다)."""
    supports_poll = True

    def __init__(self, cash=30_000.0):
        super().__init__()
        self.n = 0
        self.orders: dict[str, dict] = {}
        self.cash = cash
        self.holdings: dict[str, int] = {}
        self.daily: dict[str, list] = {}

    async def place_order(self, order):
        self.n += 1
        order.broker_id = str(self.n)
        self.orders[order.broker_id] = {"symbol": order.symbol, "side": order.side, "qty": order.qty, "filled": 0,
                                        "price": order.limit_price or 100.0, "done": False}

    def fill(self, bid, qty=None, notify=True, price=None):
        o = self.orders[bid]
        q = qty if qty is not None else o["qty"] - o["filled"]
        if price is not None:
            o["price"] = price
        o["filled"] += q
        o["done"] = o["filled"] >= o["qty"]
        d = q if o["side"] == BUY else -q
        self.holdings[o["symbol"]] = self.holdings.get(o["symbol"], 0) + d
        self.cash -= d * o["price"]
        if notify:
            self.on_fill(bid, q, o["price"], time.time())

    async def cancel_order(self, order):
        o = self.orders.get(order.broker_id)
        if o is None or o["done"]:
            raise BrokerError("already done")
        o["done"] = True

    async def poll_order(self, order):
        o = self.orders[order.broker_id]
        return o["filled"], o["price"], o["done"]

    async def lookup_order(self, order):
        return "absent", ""

    async def get_account(self):
        return AccountSnapshot(self.cash, {s: (q, 0.0) for s, q in self.holdings.items() if q > 0})

    async def get_daily_bars(self, symbol, n):
        return self.daily.get(symbol, [])[-n:]


def live_cfg(tmp_path, mode="kis-mock") -> Config:
    cfg = Config.for_market("US")
    cfg.mode, cfg.data_dir = mode, str(tmp_path)
    cfg.llm.enabled = cfg.news.enabled = cfg.learn.enabled = False
    cfg.universe = list(SYMS)
    return cfg


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def feed(e, day, hh, mm, px, syms=SYMS):
    t = US.epoch(day, hh, mm)
    for i, s in enumerate(syms):
        e.on_tick(Tick(s, t + i, px, 100, round(px - 0.01, 2), round(px + 0.01, 2)))


async def open_book(sup, br, day):
    """9종목을 100달러에 30주씩 산다(각 10% 비중, 손절가 92)."""
    e = sup.engine
    feed(e, day, 10, 0, 100.0)
    for s in SYMS:
        e._send(OrderIntent(s, BUY, 30, 100.0, "entry", {"stop_dist": 8.0, "atr": 3.0}), e.sym[s])
    await asyncio.sleep(0.02)
    for bid in list(br.orders):
        br.fill(bid)
    assert len(e.positions) == 9 and not e.pending


async def fresh(cfg, cash=30_000.0):
    br = Acct(cash)
    sup = Supervisor(cfg, br)
    sup.executor.settle_delays = (0.0, 0.01, 0.01, 0.01)
    await sup.reconcile(initial=True)
    return sup, br


async def restart(cfg, old: Acct):
    br = Acct(old.cash)
    br.holdings, br.orders, br.n, br.daily = dict(old.holdings), old.orders, old.n, old.daily
    sup = Supervisor(cfg, br)
    sup.executor.settle_delays = (0.0, 0.01, 0.01, 0.01)
    await sup.reconcile(initial=True)                   # Supervisor.run() 이 맨 처음 하는 일
    return sup, br


def sells(br: Acct) -> list:
    return [o for o in br.orders.values() if o["side"] == SELL]


async def test_restart_values_positions_at_market_not_cost(tmp_path):
    """재시작 직후 보유 종목을 취득가로 평가하면 평가이익이 통째로 사라진 것처럼 보인다.
    (이전: +12% 이익 상태에서 다시 켜면 '최대 낙폭 8% 도달'로 전량 청산, 같은 날 +3%면 '일일 손실 한도'로 전량 청산)"""
    for gain, same_day in ((1.12, False), (1.035, True)):
        cfg = live_cfg(tmp_path / f"g{gain}")
        d0 = US.now().date()
        d1 = d0 if same_day else prev_weekday(d0)
        sup, br = await fresh(cfg)
        e = sup.engine
        await open_book(sup, br, prev_weekday(d1))
        feed(e, d1, 9, 31, 100.0 * gain)
        e.mark()
        eq, peak = e.equity, e.risk.peak_equity
        assert eq > 30_000 and not e.risk.halted
        sup._save_state()
        sup.journal.close()

        sup2, br2 = await restart(cfg, br)
        e2 = sup2.engine
        assert abs(e2.equity - eq) < 0.01 and abs(e2.risk.peak_equity - peak) < 0.01
        assert not e2.risk.halted and not e2.risk.day_stop and not e2.risk.flatten_all
        feed(e2, d0, 9, 40, 100.0 * gain)                # 다시 켠 뒤 같은 가격의 시세가 들어온다
        feed(e2, d0, 9, 41, 100.0 * gain)
        await asyncio.sleep(0.02)
        assert not sells(br2) and len(e2.positions) == 9 and not e2.risk.flatten_all
        sup2.journal.close()


async def test_days_off_are_not_counted_as_todays_loss(tmp_path):
    """며칠 꺼져 있던 사이의 하락을 '오늘 하루의 손실'로 계산하지 않는다 — 장전 준비 때 전일 종가로 다시 평가한다.
    (이전: 이틀에 걸쳐 2.4% 내린 계좌를 다음 날 켜면 가격이 그대로여도 일일 손실 한도 발동 → 전량 청산)"""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    d1, d2 = prev_weekday(d0), prev_weekday(prev_weekday(d0))
    sup, br = await fresh(cfg)
    e = sup.engine
    await open_book(sup, br, d2)
    feed(e, d2, 15, 59, 98.7)
    sup._save_state()                                    # 그제 저녁에 꺼졌다(마지막 가격 98.7)
    sup.journal.close()
    hist, x = [], d0
    for i in range(30):                                  # 증권사가 주는 일봉: 어제 종가는 97.4
        x = prev_weekday(x)
        c = 97.4 if i == 0 else 98.7
        hist.append(Bar("", US.epoch(x, 9, 30), c, c + 0.5, c - 0.5, c, 1_000_000))
    hist.reverse()
    br.daily = {s: [Bar(s, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in hist] for s in SYMS}

    sup2, br2 = await restart(cfg, br)
    e2 = sup2.engine
    assert abs(e2.equity - (e2.cash + 9 * 30 * 98.7)) < 0.01     # 복원 직후에는 저장된 마지막 가격으로
    br2.now = lambda: US.epoch(d0, 9, 0)
    await sup2.day_start(d0)                                     # 장전 준비
    eq = e2.cash + 9 * 30 * 97.4
    assert abs(e2.equity - eq) < 0.01 and abs(e2.day_start_equity - eq) < 0.01 and abs(e2.risk.start_equity - eq) < 0.01
    feed(e2, d0, 9, 31, 97.4)                                    # 오늘은 전일 종가 그대로
    feed(e2, d0, 9, 32, 97.4)
    await asyncio.sleep(0.02)
    assert not e2.risk.day_stop and not e2.risk.flatten_all and not sells(br2)
    feed(e2, d0, 10, 0, 94.0)                                    # 정말로 오늘 3.5% 빠지면 한도는 정상 작동
    feed(e2, d0, 10, 1, 94.0)
    assert e2.risk.day_stop and e2.risk.flatten_all
    sup2.journal.close()


async def test_fills_while_the_bot_was_down_are_booked_before_comparing_with_the_account(tmp_path):
    """꺼져 있는 동안 체결된 주문은 '계좌와 비교하기 전에' 장부에 넣어야 한다.
    (이전: 매도가 체결돼 있으면 포지션만 지우고 대금은 안 넣음 → 자산 10% 증발로 보여 전량 청산.
           매수가 체결돼 있으면 현금을 두 번 차감 → 현금 음수)"""
    # 매도 주문이 걸린 채 꺼졌고, 꺼져 있는 동안 전량 체결
    cfg = live_cfg(tmp_path / "sell")
    d0 = US.now().date()
    sup, br = await fresh(cfg)
    e = sup.engine
    await open_book(sup, br, d0)
    feed(e, d0, 10, 5, 101.0)
    e._send(OrderIntent("S0", SELL, 30, 101.0, "take_profit"), e.sym["S0"])
    await asyncio.sleep(0.02)
    assert sup.executor.orders
    sup._save_state()
    sup.journal.close()
    bid = next(b for b, o in br.orders.items() if o["side"] == SELL)
    br.fill(bid, notify=False)
    sup2, br2 = await restart(cfg, br)
    e2 = sup2.engine
    assert "S0" not in e2.positions and len(e2.positions) == 8
    assert abs(e2.cash - (br2.cash - e2.fees_total)) < 0.01                   # 매도 대금이 장부 현금에 들어왔다
    assert e2.realized_total > 0 and not e2.risk.halted and not e2.risk.flatten_all
    sup2.journal.close()

    # 매수 주문이 걸린 채 꺼졌고, 꺼져 있는 동안 체결(계좌 현금은 이미 줄어 있다)
    cfg = live_cfg(tmp_path / "buy")
    sup, br = await fresh(cfg)
    e = sup.engine
    feed(e, d0, 10, 0, 100.0)
    e._send(OrderIntent("S1", BUY, 30, 100.0, "entry", {"stop_dist": 8.0, "atr": 3.0}), e.sym["S1"])
    await asyncio.sleep(0.02)
    sup._save_state()
    sup.journal.close()
    br.fill("1", notify=False)
    sup2, br2 = await restart(cfg, br)
    e2 = sup2.engine
    assert e2.positions["S1"].qty == 30 and abs(e2.cash - (30_000 - 3_000 * (1 + cfg.fees.commission))) < 0.01
    assert not e2.risk.halted and e2.risk.drawdown < 0.01
    sup2.journal.close()


async def test_position_gone_from_the_account_is_booked_as_a_sale_and_halts_without_liquidating(tmp_path):
    """봇이 꺼져 있는 동안 사람이 직접 판 종목: 포지션만 지우면 자산이 사라진 것처럼 보여 낙폭 한도가 발동한다.
    (이전: 10% 비중 한 종목을 손으로 팔았을 뿐인데 다시 켜자 '최대 낙폭 10.2%' → 나머지 8종목 전량 청산)"""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    sup, br = await fresh(cfg)
    e = sup.engine
    await open_book(sup, br, d0)
    eq = e.equity
    sup._save_state()
    sup.journal.close()
    br.holdings["S0"] = 0                                # 계좌에서 S0 가 없어졌다
    br.cash += 3_000
    sup2, br2 = await restart(cfg, br)
    e2 = sup2.engine
    assert "S0" not in e2.positions and len(e2.positions) == 8
    assert abs(e2.equity - eq) < 20                      # 판 것으로 기록해 자산은 그대로(수수료만 차이)
    assert e2.risk.halted and not e2.risk.flatten_all and "불일치" in e2.risk.halt_reason   # 신규 매수만 멈추고 사람을 부른다
    feed(e2, d0, 10, 30, 100.0, SYMS[1:])
    await asyncio.sleep(0.02)
    assert not sells(br2)                                # 남은 종목을 팔지 않는다
    sup2.journal.close()


async def test_stop_out_cooldown_survives_a_restart(tmp_path):
    """손실 청산 뒤 재진입 금지(봉 수 기준)는 재시작해도 이어진다. (이전: 다음 날 다시 켜면 바로 재진입 가능)"""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    sup, br = await fresh(cfg)
    e = sup.engine
    feed(e, d0, 10, 0, 100.0, ["S0"])
    e.on_fill(Fill("S0", BUY, 10, 100.0, time.time(), ctx={"stop_dist": 5.0, "atr": 2.0}))
    e.on_fill(Fill("S0", SELL, 10, 94.0, time.time(), reason="stop_loss"))
    s = e.sym["S0"]
    assert s.bar_count - s.last_loss_bar == 0
    sup._save_state()
    sup.journal.close()
    sup2, _ = await restart(cfg, br)
    s2 = sup2.engine.sym["S0"]
    assert s2.bar_count - s2.last_loss_bar == 0 < sup2.engine.p.cooldown_bars
    sup2.journal.close()


def test_each_style_keeps_its_own_store(tmp_path):
    """방식(스윙/단타)이 다르면 학습된 전략 파라미터와 기록 봉을 섞지 않는다. (이전: 같은 폴더를 써서 단타 파라미터로 스윙을 돌림)"""
    a, b = Config.for_market("US"), Config.for_market("US", "intraday")
    a.data_dir = b.data_dir = str(tmp_path)
    assert store_dir(a).name == "sim-us" and store_dir(b).name == "sim-us-intraday"
    k = Config.for_market("KRX")
    k.data_dir = str(tmp_path)
    assert store_dir(k).name == "sim"                    # 기본 조합의 폴더 이름은 그대로


def test_missed_close_does_not_lose_the_daily_bar():
    """마감 처리를 못 하고 날이 바뀌어도(절전·중단) 어제의 일봉은 지표에 들어가고, 오늘 그 봉에 대한 판단을 한다."""
    e = Engine(Config.for_market("US"), StrategyParams(), 30_000)
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    for hh, px in ((10, 100.0), (12, 104.0), (15, 103.0)):
        e.on_tick(Tick("X", US.epoch(d1, hh, 0), px, 1000))
    assert not e.sym["X"].hist                           # 장 마감 처리(flush_bars)가 호출되지 않았다
    e.on_tick(Tick("X", US.epoch(d2, 9, 30), 103.5, 500))
    s = e.sym["X"]
    assert len(s.hist) == 1 and s.hist[0][1:6] == (100.0, 104.0, 100.0, 103.0, 3000)
    assert s.hist[0][0] == US.epoch(d1, 9, 30) and s.undecided
    assert (s.o, s.h, s.l) == (103.5, 103.5, 103.5)      # 오늘 봉은 새로 시작


def test_tick_stamped_with_an_earlier_day_does_not_roll_the_day_back():
    """지난 날짜가 찍힌 시세(지연·정지 종목)가 섞여도 '날이 바뀐 것'으로 보지 않는다.
    (이전: 폴링마다 날짜가 어제↔오늘로 오가며 당일 손익 기준이 계속 초기화 → 일일 손실 한도가 영영 발동 안 함)"""
    e = Engine(Config.for_market("US"), StrategyParams(), 30_000)
    rolls = []
    e.on_day_cb = rolls.append
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    e.on_tick(Tick("A", US.epoch(d1, 15, 0), 100.0, 10))
    e.on_fill(Fill("A", BUY, 250, 100.0, US.epoch(d1, 15, 0), ctx={"stop_dist": 20.0, "atr": 5.0}))
    e.on_tick(Tick("A", US.epoch(d2, 9, 31), 100.0, 10))
    start = e.day_start_equity
    for i in range(10):
        e.on_tick(Tick("B", US.epoch(d1, 15, 30), 50.0, 0))           # 어제 시각이 찍힌 B 의 시세
        e.on_tick(Tick("A", US.epoch(d2, 9, 32) + 6 * i, 100.0 - 0.5 * i, 10))
    assert len(rolls) == 1 and e.day_start_equity == start and "B" not in e.sym
    assert e.risk.day_stop                               # 오늘 −4% → 한도는 정상 작동


def test_severe_news_exit_order_survives_to_the_next_session():
    """장 마감 뒤 확인된 심각한 악재의 청산 지시는 다음 장에 실행된다. (이전: 날이 바뀌면 지시가 지워짐)"""
    sent = []
    e = Engine(Config.for_market("US"), StrategyParams(), 30_000, execute=sent.append)
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    e.on_tick(Tick("N", US.epoch(d1, 15, 0), 100.0, 10))
    e.on_fill(Fill("N", BUY, 10, 100.0, US.epoch(d1, 15, 0), ctx={"stop_dist": 20.0, "atr": 5.0}))
    e.exit_now.update({"N", "ZZ"})                       # 장 마감 뒤 Claude 가 확인(ZZ 는 보유하지 않은 종목)
    e.on_tick(Tick("N", US.epoch(d2, 9, 30), 99.0, 10))
    assert e.exit_now == {"N"}
    assert sent and sent[-1].symbol == "N" and sent[-1].reason == "news_exit"
    assert e.export_state()["exit_now"] == ["N"]          # 재시작해도 이어진다


async def test_news_trouble_never_stops_trading(tmp_path, unused_tcp_port):
    """소식 수집은 보조 기능이다 — 이상한 문서 하나, 예외 하나 때문에 매매가 멈추면 안 된다.
    (이전: EUC-KR 로 선언된 피드 한 건에 소식 작업이 죽고, 총괄이 그것을 치명적 오류로 보고 전체를 종료)"""
    euckr = ('<?xml version="1.0" encoding="euc-kr"?><rss version="2.0"><channel><item><title>엔비디아 실적 발표</title>'
             '<link>https://example.com/a</link></item></channel></rss>').encode("euc-kr")
    assert [t for _, t, _ in parse_feed(euckr, "x")] == ["엔비디아 실적 발표"]
    assert parse_feed(b'<?xml version="1.0" encoding="x-unknown"?><rss/>', "x") == []
    assert parse_feed(b"\xff\xfe\x00 not xml", "x") == []

    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url=f"http://127.0.0.1:{unused_tcp_port}"))
    calls = []

    async def broken():
        calls.append(1)
        raise RuntimeError("boom")

    task = asyncio.create_task(sup._aux("정보수집", broken))
    await asyncio.sleep(0.05)
    assert calls and not task.done()                     # 죽지 않고 다시 시작을 기다린다
    assert any("정보수집 오류" in ev["text"] for ev in sup.engine.events)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    sup.journal.close()


async def test_feed_watch_and_periodic_save(tmp_path):
    """장이 열리는 순간의 가짜 '시세 끊김' 경고를 내지 않고, 주문 보조 모드도 장부를 주기적으로 저장한다."""
    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    alerts = []
    sup.notifier.send = alerts.append
    e = sup.engine
    sup._last_tick_wall = 1000.0                          # 장전 준비(30분 전) 이후 시세가 없었다
    sup._watch_feed(1000.0 + 1800, False)                 # 아직 장 전
    assert not e.risk.feed_stale and not sup.broker.expect_data
    sup._watch_feed(1000.0 + 1860, True)                  # 방금 장이 열렸다 → 지금부터 기다린다
    assert not e.risk.feed_stale and sup.broker.expect_data and not alerts
    sup._watch_feed(1000.0 + 1860 + sup.stale_sec + 1, True)      # 열린 뒤에도 계속 무소식이면 그때 경고
    assert e.risk.feed_stale and len(alerts) == 1
    sup._last_tick_wall = 1000.0 + 1860 + sup.stale_sec + 2
    sup._watch_feed(1000.0 + 1860 + sup.stale_sec + 3, True)
    assert not e.risk.feed_stale

    # 추적 손절가처럼 체결 없이 바뀌는 값도 주기적으로 저장된다(이전: 주문 보조 모드는 체결 때만 저장)
    e.on_tick(Tick("S0", US.epoch(US.now().date(), 10, 0), 100.0, 1))
    e.on_fill(Fill("S0", BUY, 10, 100.0, time.time(), ctx={"stop_dist": 5.0, "atr": 2.0}))
    e.positions["S0"].stop = 110.0                        # 체결 없이 추적 손절가만 올라갔다
    sup.phase, sup._save_ts = "OPEN", time.monotonic() - 61
    task = asyncio.create_task(sup._timer_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    st = json.loads((store_dir(cfg) / "state.json").read_text(encoding="utf-8"))
    assert st["engine"]["positions"][0]["stop"] == 110.0 and st["engine"]["last"]["S0"] == 100.0
    sup.journal.close()


async def test_dashboard_refuses_requests_addressed_to_another_host_name(tmp_path, unused_tcp_port):
    """DNS 리바인딩: 공격자 도메인이 127.0.0.1 을 가리키게 하면 브라우저는 '같은 출처'로 본다. 내 컴퓨터에서만 여는
    대시보드는 localhost 계열 이름으로 온 요청만 받는다. (이전: 토큰 없는 기본 설정에서 전량 청산 명령까지 통과)"""
    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    dash = Dashboard(sup, "127.0.0.1", unused_tcp_port)
    await dash.start()
    base = f"http://127.0.0.1:{unused_tcp_port}"
    evil = {"Host": f"evil.example:{unused_tcp_port}", "Origin": f"http://evil.example:{unused_tcp_port}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base + "/api/control", json={"action": "flatten"}, headers=evil) as r:
                assert r.status == 403
            async with s.get(base + "/api/state", headers=evil) as r:
                assert r.status == 403
            async with s.get(base + "/", headers=evil) as r:
                assert r.status == 403
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await s.ws_connect(base + "/ws", headers=evil)
            assert not sup.engine.risk.halted
            for host in (f"localhost:{unused_tcp_port}", f"127.0.0.1:{unused_tcp_port}"):
                async with s.get(base + "/api/state", headers={"Host": host}) as r:
                    assert r.status == 200
            async with s.post(base + "/api/control", json={"action": "halt"}) as r:
                assert r.status == 200 and sup.engine.risk.halted
    finally:
        await dash.stop()
        sup.journal.close()
    # 외부에 여는 경우(토큰 필수)에는 어떤 이름으로 와도 토큰 검사로 막는다
    dash2 = Dashboard(sup, "0.0.0.0", unused_tcp_port, token="t0k")
    await dash2.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/state", headers={"Host": "my-server.example"}) as r:
                assert r.status == 401
            async with s.get(base + "/api/state", headers={"Host": "my-server.example", "X-Token": "t0k"}) as r:
                assert r.status == 200
    finally:
        await dash2.stop()


# ====================================================================== 소식·대시보드 보안(세 번째 검토)
from types import SimpleNamespace

from aitrader.agents.news import MAX_FEED_BYTES, NewsAgent
from aitrader.llm import LLM


class SymLLM:
    """종목별로 정해 둔 판정을 돌려주는 가짜 Claude. fail=True 면 응답 없음(None)."""

    def __init__(self, by_symbol=None, default=0):
        self.enabled, self.by, self.default, self.calls, self.fail, self.raw = True, by_symbol or {}, default, [], False, None

    async def ask_json(self, role, model, effort, system, payload, schema, max_tokens=0):
        self.calls.append(payload)
        if self.fail:
            return None
        if self.raw is not None:
            return self.raw
        return {"items": [{"i": i, "impact": self.by.get(sym, self.default), "kind": "legal"}
                          for i, sym, _ in payload["items"]]}


def news_env2(llm, held=("AMD", "NVDA", "AAPL", "MSFT")):
    cfg = Config.for_market("US")
    e = Engine(cfg, StrategyParams(), 100_000)
    na = NewsAgent(cfg, llm, e, Broker())
    na.set_symbols(cfg.universe)
    t = US.epoch(date(2026, 1, 5), 10, 0)
    for sym in held:
        e.on_tick(Tick(sym, t, 100.0, 10))
        e.on_fill(Fill(sym, BUY, 10, 100.0, t, ctx={"stop_dist": 5.0, "atr": 2.0}))
    return cfg, e, na


NOW = 1794571400.0
FRAUD = "AMD plunges after SEC charges it with accounting fraud; Nvidia, Apple and Microsoft seen gaining share"


async def test_news_verdict_applies_only_to_the_symbol_that_was_asked_about():
    """한 제목에 여러 종목이 걸려도 판정은 '물어본 그 종목'에만 적용한다.
    (이전: AMD 에 대한 −2 판정이 제목에 언급된 NVDA·AAPL·MSFT 보유분까지 전부 강제 매도)"""
    llm = SymLLM({"AMD": -2, "NVDA": 1, "AAPL": 0, "MSFT": 1})
    cfg, e, na = news_env2(llm)
    na.ingest([(NOW, FRAUD, "", "x", ("AMD",))], NOW)
    it = next(iter(na.items.values()))
    assert set(it.symbols) == {"AMD", "NVDA", "AAPL", "MSFT"}
    assert await na.classify(NOW + 400) == 4
    asked = {sym for _, sym, _ in llm.calls[0]["items"]}
    assert asked == {"AMD", "NVDA", "AAPL", "MSFT"}                      # 종목마다 따로 묻는다
    assert e.exit_now == {"AMD"}                                         # 청산 지시는 AMD 만
    assert e.news["AMD"].block_until > NOW and e.news["NVDA"].block_until == 0.0   # 반사이익 종목의 매수 보류는 풀린다
    assert it.verdicts == {"AMD": -2, "NVDA": 1, "AAPL": 0, "MSFT": 1} and it.impact == -2


async def test_news_subject_is_judged_even_when_first_seen_in_another_symbols_feed():
    """엔비디아 소식이 SPY 피드에서 먼저 보여도 NVDA 에 대해 따로 판정받는다. 같은 제목이 다른 종목 피드에서
    다시 오면 그 종목도 대상에 붙는다. (이전: SPY 기준 '영향 없음' 판정으로 NVDA 매수 보류가 풀리고 청산도 누락)"""
    llm = SymLLM({"SPY": 0, "NVDA": -2})
    cfg, e, na = news_env2(llm, held=("NVDA",))
    title = "Nvidia under investigation for accounting fraud, trading halted"
    na.ingest([(NOW, title, "", "x", ("SPY",))], NOW)
    assert await na.classify(NOW + 400) == 2
    assert e.exit_now == {"NVDA"} and e.news["NVDA"].block_until > NOW
    # 제목에 종목 이름이 없는 소식: 처음엔 SPY 피드에서만 보였다가 나중에 TSLA 피드에서도 온다
    vague = "Chipmaker faces lawsuit over defective product recall"
    na.ingest([(NOW, vague, "", "x", ("SPY",))], NOW)
    assert na.ingest([(NOW, vague, "", "x", ("TSLA",))], NOW + 1) == 0   # 새 소식으로 세지는 않지만
    it = [x for x in na.items.values() if x.title == vague][0]
    assert it.symbols == ("SPY", "TSLA") and e.news["TSLA"].block_until > NOW      # TSLA 에도 반영된다


async def test_news_classifier_ignores_bad_indices_and_never_invents_verdicts_on_failure():
    """모델이 엉뚱한 번호를 돌려줘도 다른 소식에 적용되지 않고, 응답이 없을 때 판정을 지어내지 않는다.
    (이전: i=−1 이 '마지막 소식'으로 해석됨. 응답 실패 시 규칙 점수를 'Claude 판정'으로 표시)"""
    llm = SymLLM()
    cfg, e, na = news_env2(llm, held=("MSFT",))
    na.ingest([(NOW, "Nvidia wins approval for new chip", "", "x", ())], NOW)
    na.ingest([(NOW, "Microsoft announces $2B public offering", "", "x", ())], NOW)
    llm.raw = {"items": [{"i": -1, "impact": -2, "kind": "legal"}, {"i": 99, "impact": -2, "kind": "legal"},
                         {"i": "x", "impact": -2, "kind": "legal"}]}
    assert await na.classify(NOW + 400) == 0 and not e.exit_now
    llm.raw, llm.fail = None, True                                       # 응답 없음(예산 소진·장애)
    calls0, day_calls0 = len(llm.calls), na.llm_calls_day
    for k in range(9):
        assert await na.classify(NOW + 800 + 400 * k) == 0
    assert all(not it.verdicts and it.impact is None for it in na.items.values())
    assert all(v["by"] == "rule" for v in na.recent(5))                  # 화면에도 '규칙 판정'으로 남는다
    assert na.llm_off and any("Claude 소식 분류 중단" in ev["text"] for ev in e.events)
    assert len(llm.calls) == calls0 + na.MAX_FAILS                       # 끝없이 다시 보내지는 않는다
    assert na.llm_calls_day == day_calls0                                # 응답 없는 시도는 하루 횟수를 쓰지 않는다
    na.new_day()                                                         # 다음 날 장전: 예산이 새로 시작되면 다시 기회를 준다
    llm.fail = False
    llm.by = {"MSFT": -2}
    assert await na.classify(NOW + 5000) == 2 and na.llm_off == ""       # 보유 종목의 묵은 소식도 이제 판정받는다
    assert e.exit_now == {"MSFT"}


async def test_news_keeps_calls_for_held_symbols_and_judges_overnight_news():
    """하루 분류 횟수의 일부는 보유 종목 소식을 위해 남겨 두고, 보유 종목의 소식은 늦게라도 판정한다.
    (이전: 잡다한 소식으로 오후 2시쯤 60회를 다 쓰고, 장 마감 뒤의 악재는 다음 날에도 판정받지 못함)"""
    llm = SymLLM({"NVDA": -2})
    cfg, e, na = news_env2(llm, held=("NVDA",))
    cfg.llm.news_max_calls = 6
    t = NOW
    for i in range(10):                                                  # 보유하지 않은 종목의 잡다한 소식이 5분마다
        t = NOW + i * 400
        na.ingest([(t, f"Tesla update number {i} on deliveries", "", "x", ())], t)
        await na.classify(t)
    assert na.llm_calls_day == 1                                         # 6회 중 5회는 보유 종목 몫으로 남긴다(잡다한 소식에는 1회만)
    t += 400
    na.ingest([(t, "Nvidia misses estimates and slashes guidance", "", "x", ())], t)
    late = t + 6 * 3600                                                  # 장 마감 뒤 소식을 6시간 뒤에야 판정할 기회가 왔다
    assert await na.classify(late) >= 1
    assert e.exit_now == {"NVDA"}                                        # 90분이 넘었어도 보유 종목이면 판정한다


async def test_severe_news_on_a_symbol_we_do_not_hold_only_blocks_buying():
    """보유하지 않은 종목의 심각한 악재는 매수 보류로 끝낸다 — 청산 지시를 남겨 두면 나중에 산 것을 곧바로 판다."""
    llm = SymLLM({"TSLA": -2})
    cfg, e, na = news_env2(llm, held=())
    na.ingest([(NOW, "Tesla under investigation for accounting fraud", "", "x", ())], NOW)
    assert await na.classify(NOW + 400) == 1
    assert not e.exit_now and e.news["TSLA"].block_until > NOW


def test_engine_does_not_buy_a_symbol_with_a_pending_exit_order():
    """악재 청산 지시가 걸린 종목은 새로 사지 않는다."""
    cfg = Config.for_market("US", "intraday")
    sent = []
    e = Engine(cfg, StrategyParams(), 30_000, execute=sent.append)
    e.chart.evaluate = lambda *a, **k: SimpleNamespace(symbol="X", score=0.9, parts={})
    t = US.epoch(date(2026, 1, 5), 10, 0)
    e.on_tick(Tick("X", t, 100.0, 100))
    s = e.sym["X"]
    s.f.ready, s.f.close, s.f.atr = True, 100.0, 1.0
    e.exit_now.add("X")
    e._decide(s, s.f, 10 * 3600.0)
    assert not sent and e.guard.blocks["악재 뉴스"] == 1


async def test_feed_body_is_read_to_the_end_and_dtd_is_rejected(unused_tcp_port):
    """피드 본문을 '지금 도착한 만큼'만 읽으면 여러 조각으로 오는 응답이 잘려 통째로 버려진다.
    DTD(엔티티 폭탄)는 인코딩·머리말 길이와 무관하게 파서에서 거부한다."""
    items = "".join(f"<item><title>Nvidia headline number {i} about chips and demand</title>"
                    f"<link>https://example.com/{i}</link></item>" for i in range(300))
    body = f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()

    async def split(req):
        r = web.StreamResponse(headers={"Content-Type": "application/rss+xml"})
        await r.prepare(req)
        await r.write(body[:len(body) // 2])
        await asyncio.sleep(0.05)                                        # 나머지 절반은 조금 뒤에 온다
        await r.write(body[len(body) // 2:])
        await r.write_eof()
        return r

    async def huge(req):
        return web.Response(body=b"<rss>" + b"x" * (MAX_FEED_BYTES + 10) + b"</rss>")

    app = web.Application()
    app.router.add_get("/split", split)
    app.router.add_get("/huge", huge)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    cfg, e, na = news_env2(LLM(SimpleNamespace(enabled=False)), held=())
    na.session = aiohttp.ClientSession()
    try:
        await na._poll_feed(f"http://127.0.0.1:{unused_tcp_port}/split", "NVDA", time.time())
        assert len(na.items) == 300 and na.fetched == 1 and na.fetch_errors == 0
        await na._poll_feed(f"http://127.0.0.1:{unused_tcp_port}/huge", "NVDA", time.time())
        assert na.parse_errors == 1 and na.fetch_errors == 1             # 해석 못 한 것은 '소식 없음'이 아니라 오류로 센다
    finally:
        await na.session.close()
        await runner.cleanup()

    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "EXPANDED">]>'
            '<rss><channel><item><title>Nvidia &a;</title></item></channel></rss>')
    assert parse_feed(bomb.encode(), "x") == []
    assert parse_feed(bomb.replace('<?xml version="1.0"?>', "").encode("utf-16"), "x") == []          # UTF-16 으로 숨겨도
    padded = bomb.replace("?>", "?><!--" + "p" * 5000 + "-->", 1)
    assert parse_feed(padded.encode(), "x") == []                                                      # 긴 머리말 뒤에 숨겨도
    ok = '<?xml version="1.0"?><rss><channel><item><title>Nvidia fine</title></item></channel></rss>'
    assert [t for _, t, _ in parse_feed(ok.encode("utf-16"), "x")] == ["Nvidia fine"]                   # 정상 문서는 그대로


async def test_dashboard_token_check_handles_non_ascii_and_page_has_security_headers(tmp_path, unused_tcp_port):
    """영문이 아닌 토큰·입력에서 500 오류가 나지 않는다. 화면에는 틀 삽입·바깥 전송을 막는 헤더가 붙는다."""
    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    dash = Dashboard(sup, "0.0.0.0", unused_tcp_port, token="비밀번호123")
    await dash.start()
    base = f"http://127.0.0.1:{unused_tcp_port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/state", params={"token": "é"}) as r:
                assert r.status == 401                                   # (이전: 500 + 로그에 예외)
            async with s.get(base + "/api/state", params={"token": "비밀번호123"}) as r:
                assert r.status == 200                                   # (이전: 맞는 토큰인데도 500)
            async with s.get(base + "/api/state", params={"token": "비밀번호124"}) as r:
                assert r.status == 401
            async with s.get(base + "/") as r:
                h = r.headers
                assert h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer"
                assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and "default-src 'none'" in h["Content-Security-Policy"]
    finally:
        await dash.stop()
        sup.journal.close()


# ====================================================================== 전략·재생·학습(세 번째 검토)
from aitrader.agents.learner import Learner
from aitrader.backtest import Backtester
from aitrader.journal import Journal
from aitrader.params import ParamStore


def _weekdays(start: date, n: int) -> list:
    out, x = [], start
    while len(out) < n:
        if x.weekday() < 5:
            out.append(x)
        x += timedelta(days=1)
    return out


def _daily_series(sym: str, n: int = 120, start: float = 100.0) -> list:
    """오르는 구간과 쉬는 구간이 번갈아 나오는 일봉(매수 신호가 나오도록)."""
    bars, px = [], start
    for i, d in enumerate(_weekdays(date(2025, 1, 6), n)):
        nxt = px * (1.004 if (i // 20) % 2 == 0 else 0.998)
        bars.append(Bar(sym, US.epoch(d, 9, 30), px, max(px, nxt) * 1.004, min(px, nxt) * 0.996, nxt, 1_000_000))
        px = nxt
    return bars


def test_learner_replay_fills_daily_entries_at_the_next_open_not_at_the_days_low():
    """실매매 쪽 복기(학습)의 일봉 재생은 '다음 날 시가'에 사야 한다.
    (이전: 매수 시작 시각 09:35 가 재생에서는 봉의 둘째 점(저가)에 해당해, 모든 매수가 그날 저가에 체결 → 검증이 허구)"""
    cfg = Config.for_market("US")                        # 실매매 기본값 그대로(entry_start 09:35)
    cfg.universe = ["AAA"]
    bars = _daily_series("AAA")
    by_day = {US.local(b.ts).date(): b for b in bars}
    L = Learner(cfg, ParamStore(None, cfg.params), LLM(SimpleNamespace(enabled=False)), Journal(None))
    bt = L._bt(L.store.current, bars)
    trades = bt.engine.trades
    assert len(trades) >= 2
    tick = 0.01
    for t in trades:
        b = by_day[US.local(t.entry_ts).date()]
        assert abs(t.entry_price - (b.open + cfg.fees.slippage_ticks * tick)) < 1e-3      # 시가 + 슬리피지(체결가는 소수 4자리)
        assert t.entry_price > b.low + tick                                                # 그날 저가에 산 것이 아니다


def test_bar_replay_tests_a_trailing_stop_raised_by_the_high_against_the_same_bars_low():
    """봉 재생은 시가→저가→고가→저가→종가 순이다: 봉 안에서 고가를 따라 올라간 추적 손절가도 그 봉의 저가로 시험한다.
    (이전: 고가 뒤에는 종가만 봐서, 고가 뒤에 저가가 왔을 경우의 손절을 놓침 → 결과가 낙관적)"""
    cfg = Config.for_market("US")
    bt = Backtester(cfg, StrategyParams())
    e = bt.engine
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    e.on_tick(Tick("X", US.epoch(d1, 15, 0), 100.0, 10))
    e.on_fill(Fill("X", BUY, 10, 100.0, US.epoch(d1, 15, 0), ctx={"stop_dist": 5.0, "atr": 2.0}))
    pos = e.positions["X"]
    pos.trail_trigger, pos.trail_dist, pos.target = 104.0, 3.0, 200.0
    # 다음 날 봉: 시가 101, 저가 100(손절 95 위), 고가 110(추적 손절가가 107 로 올라감), 종가 108
    bt.run_bars([Bar("X", US.epoch(d2, 9, 30), 101.0, 110.0, 100.0, 108.0, 4000)])
    assert "X" not in e.positions                         # 고가 뒤의 저가(100)가 올라간 손절가(107)를 건드렸다
    t = e.trades[-1]
    assert t.exit_reason == "trailing_stop" and abs(t.exit_price - (107.0 - cfg.fees.slippage_ticks * 0.01)) < 1e-6


def test_intraday_replay_gets_daily_context_like_live():
    """분봉 재생에도 일봉 맥락(전일 종가·고가, 평균 거래량)을 준다 — 실매매와 같은 전략을 검증하기 위해.
    (이전: 단타 방식의 학습 재생은 맥락 없이 돌아 상대 거래량 기준 등을 평가할 수 없었다)"""
    cfg = Config.for_market("US", "intraday")
    cfg.universe = ["X"]
    days = _weekdays(date(2026, 1, 5), 8)
    bars = []
    for k, d in enumerate(days):
        for j in range(20):                               # 하루 20개의 3분봉
            o = 100.0 + k + j * 0.01
            bars.append(Bar("X", US.epoch(d, 9, 30) + j * 180, o, o + 0.2, o - 0.2, o + 0.05, 1000))
    seed = {"X": [Bar("X", US.epoch(date(2025, 12, 1) + timedelta(days=i), 9, 30), 90.0, 91.0, 89.0, 90.5, 50_000)
                  for i in range(10)]}
    bt = Backtester(cfg, StrategyParams()).run_bars(bars, daily_ctx=True, daily_seed=seed)
    s = bt.engine.sym["X"]
    last_full = [b for b in bars if US.local(b.ts).date() == days[-2]]       # 마지막 날 직전 거래일
    assert abs(s.prev_close - last_full[-1].close) < 1e-9                    # 전일 종가 = 전날 마지막 분봉의 종가
    assert abs(s.prev_high - max(b.high for b in last_full)) < 1e-9
    assert s.avg_dvol > 0
    bare = Backtester(cfg, StrategyParams()).run_bars(bars)                  # 맥락 없이 돌리면 비어 있다
    assert bare.engine.sym["X"].prev_close == 0.0


def test_daily_exit_is_retried_until_filled_and_a_blocked_decision_is_not_lost():
    """일봉 방식의 봉 기준 청산(추세 이탈 등)은 주문이 체결되지 않으면 그날 안에 다시 낸다.
    (이전: 하루 한 번, 10초짜리 지정가 한 번뿐 — 체결이 안 되면 다음 날까지 청산이 밀림)"""
    cfg = Config.for_market("US")
    orders = []
    e = Engine(cfg, StrategyParams(), 30_000, execute=orders.append)
    days = _weekdays(date(2026, 1, 5), 40)

    def tick(d, hh, mm, px):
        e.on_tick(Tick("X", US.epoch(d, hh, mm), px, 1000, px - 0.01, px + 0.01))

    sells = []
    for k, d in enumerate(days):
        px = 100 + k * 0.5 if k < 28 else 114.0 - (k - 27) * 1.5          # 오르다가 꺾여서 내린다
        n0 = len(orders)
        tick(d, 9, 31, px)
        tick(d, 9, 36, px)
        for o in orders[n0:]:
            if o.side == BUY:
                e.on_order_done("X", False, "skip")                       # 테스트가 직접 사므로 봇의 매수는 무시
        if k == 27:
            e.on_fill(Fill("X", BUY, 10, px, e.now, ctx={"stop_dist": 60.0, "atr": 1.0}))
        day_sells = [o for o in orders[n0:] if o.side != BUY]
        if day_sells:
            e.on_order_done("X", False, "미체결")                          # 첫 청산 주문(지정가)이 체결되지 않았다
            tick(d, 9, 37, px)                                            # 재시도 대기(3초) 뒤의 다음 틱
            sells = [o for o in orders[n0:] if o.side != BUY]
            break
        e.flush_bars()
    assert len(sells) == 2 and sells[0].reason == "trend_exit" and sells[0].limit_price > 0
    assert sells[1].reason == "trend_exit" and sells[1].limit_price == 0   # 두 번째는 반드시 체결되는 주문으로
    e.on_fill(Fill("X", SELL, 10, 100.0, e.now, reason="trend_exit"))
    assert "X" not in e.bar_exit and "X" not in e.positions               # 체결되면 지시는 사라진다

    # 판단 시각에 주문이 진행 중이면 그날의 판단을 버리지 않고 다음 틱에 한다
    e2 = Engine(cfg, StrategyParams(), 30_000, execute=lambda it: None)
    d = days[0]
    e2.on_tick(Tick("Y", US.epoch(d, 9, 31), 50.0, 10))
    s = e2.sym["Y"]
    s.undecided = True
    e2.pending["Y"] = OrderIntent("Y", BUY, 1, 50.0, "entry")
    e2.on_tick(Tick("Y", US.epoch(d, 9, 36), 50.0, 10))
    assert s.undecided                                                     # 아직 판단하지 못했다
    e2.pending.clear()
    e2.on_tick(Tick("Y", US.epoch(d, 9, 37), 50.0, 10))
    assert not s.undecided


def test_sell_is_never_blocked_by_the_price_band_even_with_a_stale_bid():
    """호가 없이 체결가만 오는 시세에서 묵은 매수호가 때문에 매도가 '주문가 이상'으로 막히지 않는다(매도는 어떤 한도로도 막지 않는다)."""
    cfg = Config.for_market("US")
    orders = []
    e = Engine(cfg, StrategyParams(), 30_000, execute=orders.append)
    t = US.epoch(date(2026, 1, 5), 10, 0)
    e.on_tick(Tick("X", t, 100.0, 10, 99.99, 100.01))                      # 호가가 있던 틱
    e.on_fill(Fill("X", BUY, 10, 100.0, t, ctx={"stop_dist": 2.0, "atr": 1.0}))
    e.positions["X"].target = 110.0
    for i, px in enumerate((104.0, 108.0, 112.5)):                         # 이후에는 호가 없이 체결가만
        e.on_tick(Tick("X", t + 60 * (i + 1), px, 10))
    assert orders and orders[-1].side == SELL and orders[-1].reason == "take_profit"
    assert abs(orders[-1].limit_price / 112.5 - 1.0) < 0.01               # 묵은 호가(99.99)가 아니라 현재가 기준
    assert not e.risk.blocks


def test_a_rollback_is_never_rolled_back():
    """되돌린 것을 다시 되돌리면 방금 버린 값이 검증 없이 되살아나 두 값 사이를 오간다."""
    cfg = Config()
    store = ParamStore(None)
    L = Learner(cfg, store, LLM(SimpleNamespace(enabled=False)), Journal(None))
    store.adopt(StrategyParams(entry_score_min=0.65), "채택: 검증 통과안")
    store.rollback("v2 기대값 -0.30R < v1 +0.20R")
    assert store.current.version == 3 and store.current.entry_score_min == 0.62

    def trades(version, r, n):
        return [SimpleNamespace(params_version=version, r_multiple=r, exit_ts=T0 + 86400.0 * (i % 5)) for i in range(n)]

    recent = trades(2, -0.3, 30) + trades(3, -0.5, 30)                     # 되돌린 뒤에도 손실이 이어진다
    assert L._maybe_rollback(recent) == "" and store.current.version == 3


async def test_restart_keeps_daily_history_and_change_timer(tmp_path):
    """재시작해도 일별 기록, 연속 손실일, '전략 변경 후 지난 날 수'가 이어진다.
    (이전: 매일 다시 켜면 변경 간격 제한과 3일 연속 손실 시 크기 축소가 한 번도 작동하지 않음)"""
    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    sup.store.adopt(StrategyParams(**{**cfg.params, "entry_score_min": 0.64}), "채택: 시험")
    changed = US.local(time.time()).date()
    for i, pnl in enumerate((-50.0, -30.0, -20.0), 1):                     # 변경 뒤 사흘 연속 손실
        sup.journal.daily(str(changed + timedelta(days=i)), 30_000 + pnl, pnl, 2, 0, 2)
    sup.journal.flush()
    sup.journal.close()
    sup2 = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    assert [x["pnl"] for x in sup2.days] == [-50.0, -30.0, -20.0]
    assert sup2.learner.day_pnls == [-50.0, -30.0, -20.0] and sup2.learner.days_since_change == 3
    assert sup2.snapshot()["days"][-1]["pnl"] == -20.0                     # 대시보드의 일별 손익도 이어진다
    sup2.journal.close()


# ====================================================================== 시장·설정(세 번째 검토)
from aitrader.agents.market import MarketAnalyst
from aitrader.config import load_config


def _closes(sym: str, last_day: date, closes: list) -> list:
    """last_day 까지의 일봉(마지막 값이 last_day 의 종가)."""
    days, x = [], last_day
    while len(days) < len(closes):
        if x.weekday() < 5:
            days.append(x)
        x -= timedelta(days=1)
    days.reverse()
    return [Bar(sym, US.epoch(d, 9, 30), c, c * 1.005, c * 0.995, c, 1_000_000) for d, c in zip(days, closes)]


async def test_failed_daily_fetch_does_not_remark_positions_with_a_stale_close(tmp_path):
    """장전 일봉 조회가 실패하면 남아 있는 '그 전날 종가'로 보유 종목을 다시 평가하지 않는다.
    (이전: 어제 −1.35% 로 마감한 계좌를 이틀 전 종가로 덮어써, 오늘 −0.9% 를 −2.05% 로 계산 → 전량 청산)"""
    cfg = live_cfg(tmp_path)
    cfg.universe = ["S0", "S1"]
    d0 = US.now().date()
    d1, d2 = prev_weekday(d0), prev_weekday(prev_weekday(d0))
    sup, br = await fresh(cfg)
    e = sup.engine
    br.daily = {s: _closes(s, d2, [100.0] * 30) for s in ("S0", "S1")}
    br.now = lambda: US.epoch(d1, 9, 0)
    await sup.day_start(d1)                                             # 어제 아침: 정상 조회(전일 종가 100)
    feed(e, d1, 9, 31, 100.0, ["S0", "S1"])
    for s in ("S0", "S1"):
        e._send(OrderIntent(s, BUY, 100, 100.0, "entry", {"stop_dist": 20.0, "atr": 3.0}), e.sym[s])
    await asyncio.sleep(0.02)
    for bid in list(br.orders):
        br.fill(bid)
    feed(e, d1, 15, 59, 98.65, ["S0", "S1"])                            # 어제 −1.35% 로 마감
    e.flush_bars()
    eq_close = e.cash + 200 * 98.65

    async def boom(symbol, n):
        raise RuntimeError("HTTP 429")

    br.get_daily_bars = boom                                            # 오늘 아침: 일봉 조회 실패
    br.now = lambda: US.epoch(d0, 9, 0)
    sup.notifier.send = lambda text: None
    await sup.day_start(d0)
    assert e.sym["S0"].last == 98.65 and e.sym["S1"].last == 98.65     # 어제 종가 그대로(이틀 전 종가 100 으로 덮어쓰지 않는다)
    assert any("일봉을 받지 못한 종목" in ev["text"] for ev in e.events)
    feed(e, d0, 9, 31, 97.75, ["S0", "S1"])                             # 오늘 실제로는 −0.9%
    feed(e, d0, 9, 32, 97.75, ["S0", "S1"])
    await asyncio.sleep(0.02)
    assert abs(e.day_start_equity - eq_close) < 0.01
    assert not e.risk.day_stop and not e.risk.flatten_all and not sells(br)
    sup.journal.close()


async def test_indicators_are_warmed_when_history_arrives_on_a_later_day(tmp_path):
    """첫날 일봉 조회가 실패한 종목도, 조회가 되는 날 과거 이력으로 지표를 채운다.
    (이전: 한 번 실패하면 재시작할 때까지 약 25거래일 동안 그 종목은 판단이 나오지 않음)"""
    cfg = live_cfg(tmp_path, "manual")
    cfg.universe = ["S0", "S1"]
    d0 = US.now().date()
    d1 = prev_weekday(d0)
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    hist = {s: _closes(s, prev_weekday(d1), [100.0 + i * 0.1 for i in range(80)]) for s in ("S0", "S1")}

    async def bars_day1(symbol, n):
        if symbol == "S1":
            raise RuntimeError("timeout")
        return hist[symbol][-n:]

    async def bars_ok(symbol, n):
        return hist[symbol][-n:]

    async def none(*a, **k):
        return []

    sup.broker.get_daily_bars, sup.broker.get_intraday_bars = bars_day1, none
    sup.broker.now = lambda: US.epoch(d1, 9, 0)
    await sup.day_start(d1)
    e = sup.engine
    assert e.sym["S0"].f.ready and e.sym["S1"].ind.n == 0
    e.on_tick(Tick("S1", US.epoch(d1, 10, 0), 108.0, 10))               # 그날의 봉이 하나 쌓인다
    e.flush_bars()
    assert e.sym["S1"].ind.n == 1 and not e.sym["S1"].f.ready
    sup.broker.get_daily_bars = bars_ok
    sup.broker.now = lambda: US.epoch(d0, 9, 0)
    await sup.day_start(d0)
    assert e.sym["S1"].f.ready and e.sym["S1"].ind.n >= 25 and len(e.sym["S1"].hist) >= 25
    sup.journal.close()


def test_params_table_overrides_single_keys_and_is_validated(tmp_path, monkeypatch):
    """config.toml 의 [params] 는 적은 항목만 덮어쓴다(나머지 방식별 기본값은 유지). 모르는 이름·숫자가 아닌 값은 거부.
    (이전: 한 줄만 적어도 스윙용 기본값 여덟 개가 단타용 값으로 되돌아감, 오타는 조용히 무시됨)"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('[params]\nstop_atr_mult = 3\n', encoding="utf-8")
    cfg = load_config()
    base = Config.for_market("US").params
    assert cfg.params == {**base, "stop_atr_mult": 3.0} and cfg.user_params == {"stop_atr_mult": 3.0}
    assert ParamStore(None, cfg.params).current.take_profit_r == 5.0     # 스윙용 익절 기준이 그대로
    for bad in ('[params]\nstop_atr_mlt = 3.0\n', '[params]\ntake_profit_r = "5"\n', '[params]\nversion = 9\n',
                'user_params = {}\n'):
        (tmp_path / "config.toml").write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            load_config()


def test_command_line_market_or_style_must_not_silently_mix_with_the_config_file(tmp_path, monkeypatch):
    """설정 파일이 전제한 시장·방식과 다른 것을 명령줄에서 주면 섞어서 돌리지 않고 멈춘다.
    (이전: 예시 파일을 복사한 채 --market krx --style intraday 를 주면 국내 장에 미국 종목·3만 '원' 자본으로 실행)"""
    monkeypatch.chdir(tmp_path)
    example = (Path(__file__).resolve().parent.parent / "config.example.toml").read_text(encoding="utf-8")
    (tmp_path / "config.toml").write_text(example, encoding="utf-8")
    assert load_config().market == "US" and load_config(market="us", style="swing").style == "swing"   # 같은 값이면 통과
    with pytest.raises(ValueError, match="--market"):
        load_config(market="krx")
    with pytest.raises(ValueError, match="--style"):
        load_config(style="intraday")
    (tmp_path / "config.toml").write_text('capital = 1000\n', encoding="utf-8")       # 시장·방식을 적지 않은 파일은 자유롭게
    k = load_config(market="krx")
    assert k.market == "KRX" and k.capital == 1000


def test_daily_style_volatility_baseline_moves_once_per_day():
    """일봉 방식의 변동성 기준선은 하루에 한 번만 움직인다.
    (이전: 매분 2%씩 따라가 하루 만에 따라잡았고, 변동성이 급증한 다음 날에도 '평상'으로 판단)"""
    cfg = Config.for_market("US")
    e = Engine(cfg, StrategyParams(), 30_000)
    ma = MarketAnalyst(cfg, LLM(SimpleNamespace(enabled=False)))
    s = e.symbol_state("SPY")
    s.f.ready, s.f.close, s.f.atr, s.f.ema_fast, s.f.ema_slow, s.f.vwap = True, 100.0, 2.0, 101.0, 100.0, 100.0
    s.day_open, s.last = 100.0, 100.0
    e.day = 100
    assert ma.measure(e)["vol_x"] == 1.0
    s.f.atr = 4.0                                                        # 다음 날: 변동성이 두 배가 됐다
    e.day = 101
    for _ in range(390):                                                 # 장중 내내 매분 측정해도
        m = ma.measure(e)
    assert m["vol_x"] >= 1.8                                             # 여전히 '확대'로 본다
    assert ma.rule_regime(m, 0.0).risk_mult <= 0.6


def test_backtest_and_bench_warm_daily_indicators():
    """일봉 방식의 합성 시세 백테스트·지연 측정은 과거 일봉으로 지표를 먼저 채운다.
    (이전: 기본 설정으로 `backtest --days 20` 을 돌리면 판단이 한 번도 나오지 않아 거래 0건)"""
    from aitrader.__main__ import _warm
    from aitrader.brokers.sim import SimMarket
    cfg = Config.for_market("US")
    mk = SimMarket(cfg.universe[:3], 7, cfg.sim.tick_sec, etfs=frozenset(), market=cfg.mkt,
                   open_hhmm=cfg.session.open, close_hhmm=cfg.session.close)
    bt = Backtester(cfg, ParamStore(None, cfg.params).current)
    _warm(bt, mk, cfg)
    assert all(bt.engine.sym[s].f.ready for s in mk.symbols)


async def test_news_judges_held_symbols_after_a_weekend_and_orders_still_pending():
    """금요일 장 마감 뒤의 악재는 월요일 아침에라도 판정한다. 매수 주문이 나가 있는 동안 온 악재도 놓치지 않는다.
    같은 회사를 'NVIDIA'·'Nvidia' 로 달리 적어도 같은 종목으로 본다."""
    llm = SymLLM({"NVDA": -2, "TSLA": -2})
    cfg, e, na = news_env2(llm, held=("NVDA",))
    cfg.llm.news_max_calls = 0                                           # 금요일: 오늘 횟수를 다 썼다
    na.ingest([(NOW, "NVIDIA misses estimates and slashes guidance", "", "x", ())], NOW)
    it = next(iter(na.items.values()))
    assert it.symbols == ("NVDA",)                                       # 대문자로 적힌 회사 이름도 찾는다
    assert await na.classify(NOW + 400) == 0 and not e.exit_now
    cfg.llm.news_max_calls = 60
    na.new_day()                                                         # 월요일 장전(65시간 뒤)
    assert await na.classify(NOW + 65 * 3600) == 1 and e.exit_now == {"NVDA"}

    # 매수 주문이 아직 체결되지 않은 종목에 심각한 악재 → 체결되면 곧바로 정리되도록 지시를 건다
    t = NOW + 66 * 3600
    e.pending["TSLA"] = OrderIntent("TSLA", BUY, 5, 100.0, "entry")
    na.ingest([(t, "Tesla under investigation for accounting fraud", "", "x", ())], t)
    assert await na.classify(t + 400) == 1 and "TSLA" in e.exit_now
    assert na.find_symbols("Is meta the new normal for spy novels?") == ()     # 종목 코드는 대소문자를 구분한다


async def test_symbol_attached_later_still_gets_its_own_verdict():
    """제목에 종목 이름이 없는 소식이 여러 종목의 피드에서 차례로 올 때, 나중에 붙은 종목도 따로 판정받는다."""
    llm = SymLLM({"MSFT": -2})
    cfg, e, na = news_env2(llm, held=("MSFT",))
    title = "Software giant faces lawsuit over defective product recall"
    na.ingest([(NOW, title, "", "x", ("SPY",))], NOW)
    assert await na.classify(NOW + 400) == 1
    na.ingest([(NOW, title, "", "x", ("QQQ",))], NOW + 500)
    assert await na.classify(NOW + 900) == 1
    na.ingest([(NOW, title, "", "x", ("MSFT",))], NOW + 1000)            # 세 번째로 붙은 보유 종목
    assert await na.classify(NOW + 1400) == 1
    assert e.exit_now == {"MSFT"}


# ====================================================================== 그 밖에 관찰된 사항(세 번째 검토)
async def test_cross_site_api_requests_and_odd_websocket_messages(tmp_path, unused_tcp_port):
    """다른 사이트의 화면이 사용자의 브라우저로 상태 조회를 반복시키지 못한다(조회마다 매매 루프가 전체 상태를 만든다).
    객체가 아닌 웹소켓 메시지에도 연결이 끊기지 않는다."""
    cfg = live_cfg(tmp_path, "manual")
    sup = Supervisor(cfg, ManualBroker(cfg, base_url="http://127.0.0.1:9"))
    dash = Dashboard(sup, "127.0.0.1", unused_tcp_port)
    await dash.start()
    base = f"http://127.0.0.1:{unused_tcp_port}"
    try:
        async with aiohttp.ClientSession() as s:
            for site in ("cross-site", "same-site"):
                async with s.get(base + "/api/state", headers={"Sec-Fetch-Site": site}) as r:
                    assert r.status == 403
            async with s.get(base + "/api/state", headers={"Sec-Fetch-Site": "same-origin"}) as r:
                assert r.status == 200
            async with s.get(base + "/", headers={"Sec-Fetch-Site": "cross-site"}) as r:
                assert r.status == 200                   # 다른 곳의 링크·즐겨찾기로 화면을 여는 것은 된다
            async with s.ws_connect(base + "/ws") as ws:
                await ws.receive_json(timeout=3)
                await ws.send_str("[]")                  # 객체가 아닌 JSON
                await ws.send_str("7")
                await ws.send_json({"sel": "NVDA"})
                msg = await ws.receive_json(timeout=3)   # 연결이 살아 있고 계속 온다
                assert "equity" in msg
    finally:
        await dash.stop()
        sup.journal.close()


def test_llm_daily_budget_survives_a_restart(tmp_path):
    """하루 토큰 예산은 재시작해도 이어진다(일지에 남은 오늘 사용량을 이어받는다).
    (이전: 다시 켤 때마다 0부터 — 오류로 죽고 다시 뜨기를 반복하면 하루 한도가 무의미)"""
    j = Journal(tmp_path / "journal.db")
    j.llm_usage(time.time() - 3 * 86400, "news", "claude-opus-5-5", 9000, 900, 0, 0, 0.5)     # 사흘 전 것은 세지 않는다
    j.llm_usage(time.time(), "news", "claude-opus-5-5", 1000, 200, 300, 0, 0.01)
    j.llm_usage(time.time(), "market", "claude-opus-5-5", 500, 100, 0, 0, 0.004)
    j.flush()
    llm = LLM(SimpleNamespace(enabled=False, daily_token_budget=2500), j)
    llm.new_day(time.strftime("%Y-%m-%d"))
    assert llm.tokens_today == 2100 and llm.calls_today == 2 and abs(llm.cost_today - 0.014) < 1e-9
    assert llm.budget_left() == 400
    llm.new_day("2099-01-01")                            # 날짜가 바뀌면 새로 시작
    assert llm.tokens_today == 0
    j.close()
