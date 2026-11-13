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
