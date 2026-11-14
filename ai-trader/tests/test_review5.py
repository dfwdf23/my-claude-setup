"""다섯 번째 독립 검토(주문 집행기·증권사 어댑터·재시작)에서 재현된 결함의 회귀 테스트.

각 테스트는 고치기 전에 실제로 장부와 계좌가 어긋나던 상황을 그대로 재현한다.
"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from aiohttp import web

from aitrader.brokers.base import Broker, BrokerError, BrokerUnknown
from aitrader.brokers.kis import KISBroker
from aitrader.brokers.kis_overseas import KISOverseasBroker
from aitrader.config import Config
from aitrader.engine import Engine
from aitrader.executor import Executor
from aitrader.market import US
from aitrader.models import BUY, SELL, Bar, Fill, Order, OrderIntent, Tick
from aitrader.params import StrategyParams
from aitrader.supervisor import Supervisor

from test_kis_overseas import os_cfg
from test_review2 import SYMS, Acct, FakeOS2, feed, fresh, live_cfg, open_book, prev_weekday

WALL = US.epoch(date(2026, 3, 11), 11, 0)       # 집행기가 보는 '실제 시각'을 장중(수요일 11:00 뉴욕)으로 고정한다
KST = timezone(timedelta(hours=9))


# ====================================================================== 재시작: 계좌 비교와 미확정 주문
async def _down_with_resting_sell(tmp_path, followed: bool):
    """10주 보유 + 10주 매도 주문이 걸린 채 꺼지고, 꺼져 있는 동안 5주가 체결된 상황을 만든다."""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    sup.executor.wall = lambda: WALL
    e, ex = sup.engine, sup.executor
    sym = SYMS[0]
    e.on_tick(Tick(sym, US.epoch(US.now().date(), 10, 0), 100.0, 10, 99.9, 100.1))
    e._send(OrderIntent(sym, BUY, 10, 100.0, "entry", {"stop_dist": 3.0, "atr": 1.5}), e.sym[sym])
    await asyncio.sleep(0.05)
    br.fill("1")
    await asyncio.sleep(0.02)
    e._send(OrderIntent(sym, SELL, 10, 0.0, "stop_loss", {}), e.sym[sym])
    await asyncio.sleep(0.05)
    if followed:                                             # 취소로 끝내고 뒤늦은 체결을 지켜보던 중에 꺼졌다
        ex.follow_delays = (100.0,)
        o = next(iter(ex.orders.values()))
        o.status = "CANCELING"
        await ex._cancel(o)
        br.orders["2"]["done"] = False                       # 취소 접수 직전의 체결이 아직 내역에 반영되지 않은 상태
        sup._save_state()
    st = json.loads((sup.dir / "state.json").read_text())
    assert [(d["side"], d["status"], d["broker_id"]) for d in st["orders"]] == [(SELL, "CANCELED" if followed else "ACK", "2")]
    ex.shutdown()
    sup.journal.close()
    br.fill("2", 5, notify=False)                            # 꺼져 있는 동안 5주 매도 체결
    if followed:
        br.orders["2"]["done"] = True
    return cfg, br, sym


async def test_restart_does_not_book_the_same_sale_twice_for_an_unsettled_order(tmp_path):
    """재시작 때, 체결 수량을 아직 확정하지 못한 이전 주문이 걸린 종목은 계좌 비교를 미룬다.
    (이전: 계좌에 5주가 모자란 것을 '판 것'으로 정리한 뒤, 조회가 되살아나 그 주문의 5주 체결이 확인되면 한 번 더 반영
           → 장부 0주인데 계좌에는 5주가 남고, 현금도 두 번 들어온 것으로 계산)"""
    for followed in (False, True):
        cfg, old, sym = await _down_with_resting_sell(tmp_path / f"f{int(followed)}", followed)
        nb = Acct(old.cash)
        nb.holdings, nb.orders, nb.n = dict(old.holdings), old.orders, old.n
        real, up = nb.poll_order, [False]

        async def poll(o, real=real, up=up):
            if not up[0]:
                raise asyncio.TimeoutError()                 # 다시 켠 직후에는 주문 조회가 안 된다(계좌 조회는 된다)
            return await real(o)

        nb.poll_order = poll
        sup = Supervisor(cfg, nb)
        e, ex = sup.engine, sup.executor
        ex.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
        ex.follow_delays = (0.02, 0.05)
        ex.stuck_poll_sec = 0.0
        ex.wall = lambda: WALL + 60
        await sup.reconcile(initial=True)
        assert e.positions[sym].qty == 10, followed          # 아직 '판 것'으로 정리하지 않는다
        assert ex.unsettled() == {sym}
        assert any("계좌 대사를 미룹니다" in ev["text"] for ev in e.events if ev["kind"] == "risk")
        up[0] = True                                         # 주문 조회가 되살아난다
        ex.on_timer(ex.clock())
        await asyncio.sleep(0.3)
        assert e.positions[sym].qty == 5 == nb.holdings[sym], followed     # 5주 체결이 한 번만 반영된다
        assert abs(e.cash - nb.cash) < 5.0                   # 현금도 맞는다(차이는 수수료뿐)
        ex.shutdown()
        sup.journal.close()


async def test_periodic_reconcile_waits_while_a_cancelled_order_is_still_being_watched(tmp_path):
    """취소 뒤 뒤늦은 체결을 지켜보는 중인 종목은 주기적 계좌 대사에서도 건너뛴다(그 체결이 곧 조회로 들어온다)."""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    e, ex = sup.engine, sup.executor
    await open_book(sup, br, US.now().date())
    sym = SYMS[0]
    br.holdings[sym] = 25                                    # 계좌에는 5주가 적다(취소 직전에 체결된 매도)
    ex._following["77"] = Order(77, sym, SELL, 30, 0.0, "stop_loss", {}, 0.0, status="CANCELED", broker_id="77", filled=0)
    for _ in range(3):
        await sup.reconcile()
    assert e.positions[sym].qty == 30 and not e.risk.halted  # 지켜보는 동안에는 '판 것'으로 정리하지 않는다
    ex._following.clear()                                    # 추적이 끝났는데도 여전히 모자라면 그때 맞춘다
    for _ in range(2):
        await sup.reconcile()
    assert e.positions[sym].qty == 25 and e.risk.halted
    sup.journal.close()


async def test_account_holding_more_than_the_ledger_is_reported_once(tmp_path):
    """계좌에 장부보다 많은 수량이 있으면 알린다(봇이 모르는 체결일 수 있다 — 그 주식에는 손절이 없다).
    같은 내용을 켤 때마다 되풀이하지는 않고, 수량이 달라지면 다시 알린다. 멈추거나 팔지는 않는다."""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    await open_book(sup, br, US.now().date())
    sup.symbols = list(SYMS)
    sup._save_state()
    sup.journal.close()

    def reborn(extra):
        nb = Acct(br.cash)
        nb.holdings, nb.orders, nb.n = dict(br.holdings), br.orders, br.n
        nb.holdings[SYMS[1]] += extra
        return Supervisor(cfg, nb), nb

    def notes(s):
        return [ev["text"] for ev in s.engine.events if ev["kind"] == "risk" and "더 있습니다" in ev["text"]]

    sup2, nb = reborn(7)
    sup2.symbols = list(SYMS)
    await sup2.reconcile(initial=True)
    assert len(notes(sup2)) == 1 and f"{SYMS[1]}: 계좌에 장부보다 7주" in notes(sup2)[0]
    assert sup2.engine.positions[SYMS[1]].qty == 30 and not sup2.engine.risk.halted      # 장부는 그대로, 정지도 없음
    sup2.journal.close()

    sup3, _ = reborn(7)                                      # 다시 켜도 같은 내용은 또 알리지 않는다
    await sup3.reconcile(initial=True)
    assert notes(sup3) == []
    sup3.journal.close()

    sup4, _ = reborn(12)                                     # 수량이 달라지면 다시 알린다
    await sup4.reconcile(initial=True)
    assert len(notes(sup4)) == 1 and "12주" in notes(sup4)[0]
    sup4.journal.close()


# ====================================================================== 재시작 정리: 취소로 저장된 주문
class _NoFlag(Broker):
    """취소는 접수하지만 취소된 주문에 '끝남' 표시를 주지 않는 증권사."""
    supports_poll = True

    def __init__(self, cum):
        super().__init__()
        self.cum, self.cancels = cum, 0

    async def cancel_order(self, order):
        self.cancels += 1

    async def poll_order(self, order):
        return self.cum, 100.0 if self.cum else 0.0, False


def _env(br):
    e = Engine(Config.for_market("US"), StrategyParams(), 30_000)
    ex = Executor(br, e, 10.0)
    ex.settle_delays = (0.0, 0.01, 0.01, 0.01, 0.01, 0.01)
    ex.follow_delays = (0.02, 0.05)
    ex.stuck_poll_sec = 1e9
    ex.wall = lambda: WALL
    return e, ex


def _saved(status, broker_id="1", filled=0):
    return [{"id": 1, "symbol": "NVDA", "side": BUY, "qty": 10, "limit_price": 100.0, "reason": "entry",
             "ctx": {"stop_dist": 3.0}, "created_ts": WALL - 60, "wall_ts": WALL - 60, "status": status,
             "broker_id": broker_id, "broker_org": "", "filled": filled, "avg_price": 0.0}]


async def test_recover_treats_a_saved_cancelled_order_that_is_still_open_as_live():
    """'취소됨'으로 저장된 주문이라도 조회에 아직 열려 있다고 나오면 살아 있는 주문처럼 다시 취소하고 확인한다.
    그 뒤에 오는 체결 통보는 '지금까지 장부에 넣은 수량' 다음부터 센다.
    (이전: 조회 한 번으로 2주만 넣고 끝 — 취소도 추적도 없고, 통보는 저장된 수량(0주)부터 세어 3주 체결 통보에서 1주만 반영)"""
    br = Acct()
    br.orders["1"] = {"symbol": "NVDA", "side": BUY, "qty": 10, "filled": 0, "price": 100.0, "done": False}
    br.n = 1
    br.fill("1", 2, notify=False)                            # 꺼져 있는 동안 2주 체결, 주문은 아직 살아 있다
    e, ex = _env(br)
    assert await ex.recover(_saved("CANCELED")) == 1
    assert e.positions["NVDA"].qty == 2 and br.orders["1"]["done"]       # 살아 있던 주문을 취소했다
    assert not ex.orders and not e.pending and not e.risk.halted
    ex.shutdown()

    br = _NoFlag(cum=2)                                      # 취소된 주문에 '끝남' 표시를 주지 않는 증권사
    e, ex = _env(br)
    assert await ex.recover(_saved("CANCELED")) == 1
    assert br.cancels == 1 and e.positions["NVDA"].qty == 2 and ex.unsettled() == {"NVDA"}   # 얼마간 더 지켜본다
    ex.on_fill("1", 3, 100.0, time.time())                   # 다시 켠 뒤의 통보 3주 → 누적 5주
    assert e.positions["NVDA"].qty == 5
    ex.shutdown()


# ====================================================================== 일봉 판단: 중복·누락
def _flat(d0: date) -> dict:
    hist, x = [], d0
    for _ in range(40):
        x = prev_weekday(x)
        hist.append(Bar("", US.epoch(x, 9, 30), 100.0, 100.5, 99.5, 100.0, 1_000_000))
    hist.reverse()
    return {s: [Bar(s, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in hist] for s in SYMS}


async def _restart(cfg, old):
    nb = Acct(old.cash)
    nb.holdings, nb.orders, nb.n, nb.daily = dict(old.holdings), old.orders, old.n, old.daily
    sup = Supervisor(cfg, nb)
    await sup.reconcile(initial=True)
    return sup, nb


async def test_daily_decision_is_not_repeated_after_a_same_day_restart(tmp_path):
    """같은 날 다시 켜도 일봉 판단을 두 번 하지 않는다(보유 일수가 두 번 세어지지 않는다)."""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    sup, br = await fresh(cfg)
    e = sup.engine
    br.daily = _flat(d0)
    br.now = lambda: US.epoch(d0, 9, 0)
    await sup.day_start(d0)
    feed(e, d0, 9, 31, 100.0)
    for s in SYMS[:2]:                                       # 판단 시각(09:35) 전에 이미 보유 중인 두 종목
        e._send(OrderIntent(s, BUY, 30, 100.0, "entry", {"stop_dist": 8.0, "atr": 3.0}), e.sym[s])
    await asyncio.sleep(0.02)
    for bid in list(br.orders):
        br.fill(bid)
    feed(e, d0, 9, 36, 100.0)                                # 09:35 이후 첫 시세 → 일봉 판단
    await asyncio.sleep(0.02)
    held = [e.positions[s].bars_held for s in SYMS[:2]]
    assert held == [1, 1] and e.decided[SYMS[0]] == e.day and e.export_state()["decided"][SYMS[0]] == e.day
    sup._save_state()
    sup.journal.close()

    sup2, br2 = await _restart(cfg, br)
    e2 = sup2.engine
    br2.now = lambda: US.epoch(d0, 9, 50)                    # 같은 날 09:50 에 다시 켰다(장 초반)
    await sup2.day_start(d0)
    assert not any(e2.sym[s].undecided for s in SYMS)        # 오늘 판단은 이미 끝났다
    feed(e2, d0, 9, 51, 100.0)
    await asyncio.sleep(0.02)
    assert [e2.positions[s].bars_held for s in SYMS[:2]] == [1, 1]
    sup2.journal.close()


async def test_late_start_still_evaluates_exits_for_held_positions(tmp_path):
    """장 시작 30분이 지나 켜도 보유 종목의 일봉 청산 판단(추세 이탈·보유 기간)은 한다. 새 매수 판단만 건너뛴다.
    (이전: 늦게 켠 날에는 보유 종목의 봉 기준 청산 판단도 통째로 건너뜀)"""
    cfg = live_cfg(tmp_path)
    d0 = US.now().date()
    d1 = prev_weekday(d0)
    sup, br = await fresh(cfg)
    e = sup.engine
    feed(e, d1, 10, 0, 100.0)
    for s in SYMS[:2]:                                       # 어제 두 종목을 샀다
        e._send(OrderIntent(s, BUY, 30, 100.0, "entry", {"stop_dist": 8.0, "atr": 3.0}), e.sym[s])
    await asyncio.sleep(0.02)
    for bid in list(br.orders):
        br.fill(bid)
    feed(e, d1, 15, 59, 100.0)
    sup._save_state()
    sup.journal.close()
    br.daily = _flat(d0)

    sup2, br2 = await _restart(cfg, br)
    e2 = sup2.engine
    br2.now = lambda: US.epoch(d0, 11, 0)                    # 오늘 11시에 켰다
    await sup2.day_start(d0)
    assert [e2.sym[s].undecided for s in SYMS[:3]] == [True, True, False]    # 보유 종목만 판단 대기
    feed(e2, d0, 11, 1, 100.0)
    await asyncio.sleep(0.02)
    assert [e2.positions[s].bars_held for s in SYMS[:2]] == [1, 1] and not e2.sym[SYMS[0]].undecided
    sup2.journal.close()


# ====================================================================== 증권사 어댑터: 주문 찾기·취소
def _domestic(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    k = cfg.kis
    k.env, k.app_key, k.app_secret, k.account, k.hts_id = "mock", "AK", "SEC", "12345678", "HTS"
    return KISBroker(cfg, "http://127.0.0.1:9", "ws://127.0.0.1:9")


def _drow(odno, at: datetime, orgn="", name="현금매수", ccld="0", rmn="0", cncl="N", qty="7"):
    return {"odno": odno, "orgn_odno": orgn, "pdno": "005930", "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": name,
            "ord_qty": qty, "ord_dt": at.strftime("%Y%m%d"), "ord_tmd": at.strftime("%H%M%S"), "tot_ccld_qty": ccld,
            "avg_prvs": "70000" if ccld != "0" else "0", "rmn_qty": rmn, "cncl_yn": cncl, "rjct_qty": "0",
            "ord_gno_brno": "06010"}


def _serve(br, rows, seen):
    async def fake_call(method, path, tr, q, cont="", retry=True):
        seen.append((q["INQR_STRT_DT"], q["INQR_END_DT"]))
        return {"output1": [r for r in rows if q["INQR_STRT_DT"] <= r["ord_dt"] <= q["INQR_END_DT"]]}, ""
    br._call = fake_call


async def test_domestic_lookup_searches_the_day_the_order_was_sent(tmp_path):
    """접수 여부를 모르는 주문은 '그 주문을 낸 날'의 주문 내역에서 찾는다.
    (이전: 항상 오늘 것만 조회 → 다음 날 다시 켜면 어제 접수·체결된 주문을 '없음'으로 보고 버림 —
           계좌에는 주식이 있는데 장부에는 없고, 아무도 알려 주지 않음)"""
    br = _domestic(tmp_path)
    now = datetime.now(KST)
    made = (now - timedelta(days=1)).replace(hour=15, minute=10, second=0, microsecond=0)    # 어제 15:10 에 낸 주문
    rows, seen = [_drow("0000012345", made + timedelta(seconds=2), ccld="7")], []
    _serve(br, rows, seen)
    o = Order(1, "005930", BUY, 7, 70000.0, "entry", {}, made.timestamp(), wall_ts=made.timestamp())
    assert await br.lookup_order(o) == ("found", "12345")
    assert seen[-1][0] == made.strftime("%Y%m%d")           # 어제부터 조회했다

    # 조회로 확인할 수 없을 만큼 오래된 주문은 '없음'이 아니라 '모름'(집행기가 잠그고 사람을 부른다)
    old = now - timedelta(days=40)
    o2 = Order(2, "005930", BUY, 7, 70000.0, "entry", {}, old.timestamp(), wall_ts=old.timestamp())
    assert await br.lookup_order(o2) == ("unknown", "")

    # 그 주문을 낸 무렵에 접수된 행만 후보다 — 두 시간 뒤에 사람이 직접 낸 같은 주문은 아니다
    br3 = _domestic(tmp_path)
    t = now.replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _serve(br3, [_drow("0000000900", t + timedelta(hours=2))], [])
    o3 = Order(3, "005930", BUY, 7, 70000.0, "entry", {}, t.timestamp(), wall_ts=t.timestamp())
    assert await br3.lookup_order(o3) == ("absent", "")


async def test_domestic_lookup_ignores_cancel_rows(tmp_path):
    """정정·취소 주문은 주문 내역에 같은 종목·수량의 별도 행으로 남는다 — 그 행을 '모르는 주문'으로 세지 않는다.
    (이전: 시간 초과 → 취소 → 같은 수량으로 다시 주문(응답 유실)이라는 평범한 흐름에서 취소 행과 새 주문이 둘 다
           후보가 되어 영영 '모름'으로 남거나, 새 주문이 접수되지 않았을 때 취소 행을 그 주문으로 착각)"""
    br = _domestic(tmp_path)
    d = (datetime.now(KST) - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    br._known["100"] = d.timestamp()                         # 봇의 첫 주문(10:00) — 시간 초과로 10:01 에 취소
    rows = [_drow("0000000100", d, cncl="Y"), _drow("0000000101", d + timedelta(minutes=1), orgn="0000000100", name="매수취소")]
    _serve(br, rows, [])
    sent = d + timedelta(minutes=3)                          # 10:03 에 같은 수량으로 다시 주문 — 응답 유실
    o = Order(2, "005930", BUY, 7, 70000.0, "entry", {}, sent.timestamp(), wall_ts=sent.timestamp())
    assert await br.lookup_order(o) == ("absent", "")        # 새 주문이 접수되지 않았다면: 취소 행을 대신 집지 않는다
    rows.append(_drow("0000000102", sent + timedelta(seconds=1), rmn="7"))
    assert await br.lookup_order(o) == ("found", "102")      # 접수됐다면: 그 주문을 찾는다


class _CancelErr(FakeOS2):
    """취소 요청에 정해 둔 오류 코드로 답하는 가짜 서버."""
    code = ""

    async def cancel(self, req):
        if self.code:
            return web.json_response({"rt_cd": "1", "msg_cd": self.code, "msg1": "취소할 수 없습니다"})
        return web.json_response({"rt_cd": "0", "output": {"ODNO": "0030100099"}})


async def test_gateway_level_cancel_rejection_is_not_treated_as_nothing_to_cancel(tmp_path, unused_tcp_port):
    """취소 요청이 게이트웨이 단계에서 거절되면(호출 한도 초과 등, 코드 EGW…) '취소할 것이 없다'가 아니라
    '취소 요청이 처리되지 않았다'는 뜻이다 → 명시적 거부(BrokerError)가 아니라 결과 모름(BrokerUnknown)으로 올린다.
    업무 단계의 거부는 그대로 BrokerError. 성공하면 취소 주문 자체의 번호를 '아는 주문'에 넣는다."""
    fake = _CancelErr()
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    br = KISOverseasBroker(os_cfg(tmp_path), f"http://127.0.0.1:{unused_tcp_port}", f"ws://127.0.0.1:{unused_tcp_port}")
    br.rl.interval = 0.0
    try:
        await br.start()
        br.last["NVDA"] = Tick("NVDA", 0.0, 166.19, 1, 166.18, 166.20)
        o = Order(1, "NVDA", BUY, 7, 166.25, "entry", {}, time.time(), wall_ts=time.time())
        await br.place_order(o)
        fake.code = "EGW00201"
        with pytest.raises(BrokerUnknown):
            await br.cancel_order(o)
        fake.code = "APBK0344"
        with pytest.raises(BrokerError) as err:
            await br.cancel_order(o)
        assert err.value.code == "APBK0344"
        fake.code = ""
        await br.cancel_order(o)
        assert "30100099" in br._known
        # 조회 범위(최대 7일)보다 오래된 주문의 접수 여부는 '없음'이 아니라 '모름'
        old = Order(2, "NVDA", BUY, 7, 166.25, "entry", {}, time.time() - 8 * 86400, wall_ts=time.time() - 8 * 86400)
        assert await br.lookup_order(old) == ("unknown", "")
    finally:
        await br.stop()
        await runner.cleanup()


# ====================================================================== 알림·상태 저장
async def test_many_alerts_at_once_end_with_a_summary_line(tmp_path):
    """경고가 한꺼번에 많이 나오면 최근 5건을 보내고, 나머지가 있었다는 것을 한 줄로 알린다(조용히 버리지 않는다)."""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    sent = []
    sup.notifier.send = sent.append
    sup._forward_alerts()
    sent.clear()
    for i in range(7):
        sup.engine.emit("risk", "", f"경고 {i}")
    sup._forward_alerts()
    assert sent[:5] == [f"⚠ 경고 {i}" for i in range(2, 7)] and len(sent) == 6 and "2건" in sent[5]
    sup.journal.close()


async def test_state_save_survives_values_json_cannot_encode(tmp_path):
    """주문 맥락에 JSON 으로 못 쓰는 값이 섞여 있어도 상태 저장은 되고, 타이머 루프를 죽이는 예외가 나지 않는다."""
    cfg = live_cfg(tmp_path)
    sup, br = await fresh(cfg)
    await open_book(sup, br, US.now().date())
    sup.engine.positions[SYMS[0]].ctx["odd"] = {1, 2, 3}      # 집합은 JSON 으로 쓸 수 없다
    sup._save_state()
    st = json.loads((sup.dir / "state.json").read_text())
    assert len(st["engine"]["positions"]) == 9 and not sup._save_failed
    sup.journal.close()
