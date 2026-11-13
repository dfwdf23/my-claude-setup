"""두 번째 독립 검토에서 재현된 결함의 회귀 테스트 — 주문 집행과 증권사 연동.

각 테스트는 '고치기 전에는 장부와 계좌가 어긋나던' 상황을 그대로 재현한다.
"""
import asyncio
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
    ex.stop_background()


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
