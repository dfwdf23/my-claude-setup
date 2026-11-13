"""KIS 해외주식 연동 검증 — 공식 예제의 형식을 흉내 낸 가짜 서버 상대(실제 서버 호출 없음)."""
import asyncio
import base64
import json
from datetime import date

import pytest
from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from aitrader.brokers.base import BrokerError
from aitrader.brokers.kis_overseas import KISOverseasBroker
from aitrader.config import Config
from aitrader.market import US, is_trading_day
from aitrader.models import BUY, SELL, Order, Tick

KEY, IV = "k" * 32, "i" * 16


def encrypt(plain: str) -> str:
    p = padding.PKCS7(128).padder()
    data = p.update(plain.encode()) + p.finalize()
    e = Cipher(algorithms.AES(KEY.encode()), modes.CBC(IV.encode())).encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()


def trade_record(sym, hms, last, bid, ask, evol, tvol, mtyp="1", key="DNAS", with_rsym=True) -> str:
    f = [""] * 26
    f[0], f[1], f[2], f[3], f[4], f[5] = key + sym, sym, "4", "20260105", "20260105", hms
    f[11], f[15], f[16], f[19], f[20], f[25] = last, bid, ask, evol, tvol, mtyp
    return "^".join(f if with_rsym else f[1:])


class FakeOS:
    def __init__(self, env="mock"):
        self.env = env
        self.orders, self.cancels, self.subs = [], [], []
        self.reject_sell_tr = ""          # 이 TR로 온 매도는 거부(모의 매도 TR 불확실성 재현)
        self.psamount_ok = True
        self.calls = {}
        app = web.Application()
        r = app.router
        r.add_post("/oauth2/tokenP", self.token)
        r.add_post("/oauth2/Approval", self.approval)
        r.add_post("/uapi/overseas-stock/v1/trading/order", self.order)
        r.add_post("/uapi/overseas-stock/v1/trading/order-rvsecncl", self.cancel)
        r.add_get("/uapi/overseas-stock/v1/trading/inquire-ccnl", self.ccnl)
        r.add_get("/uapi/overseas-stock/v1/trading/inquire-balance", self.balance)
        r.add_get("/uapi/overseas-stock/v1/trading/inquire-psamount", self.psamount)
        r.add_get("/uapi/overseas-stock/v1/trading/inquire-present-balance", self.present)
        r.add_get("/uapi/overseas-price/v1/quotations/price", self.price)
        r.add_get("/uapi/overseas-price/v1/quotations/dailyprice", self.daily)
        r.add_get("/uapi/overseas-price/v1/quotations/news-title", self.news)
        r.add_get("/tryitout", self.ws)
        self.app = app

    def hit(self, req, trs):
        self.calls[req.path] = self.calls.get(req.path, 0) + 1
        assert req.headers["authorization"] == "Bearer T" and req.headers["custtype"] == "P"
        assert req.headers["tr_id"] in trs, (req.path, req.headers["tr_id"])
        return req.headers["tr_id"]

    async def token(self, req):
        return web.json_response({"access_token": "T", "expires_in": 86400})

    async def approval(self, req):
        return web.json_response({"approval_key": "APPROVAL"})

    async def order(self, req):
        tr = self.hit(req, ("VTTT1002U", "VTTT1001U", "VTTT1006U", "TTTT1002U", "TTTT1006U"))
        b = await req.json()
        assert set(b) == {"CANO", "ACNT_PRDT_CD", "OVRS_EXCG_CD", "PDNO", "ORD_QTY", "OVRS_ORD_UNPR", "CTAC_TLNO",
                          "MGCO_APTM_ODNO", "SLL_TYPE", "ORD_SVR_DVSN_CD", "ORD_DVSN"}
        assert all(isinstance(v, str) for v in b.values()) and b["ORD_DVSN"] == "00" and b["ORD_SVR_DVSN_CD"] == "0"
        assert b["OVRS_EXCG_CD"] in ("NASD", "NYSE", "AMEX")           # 주문은 4글자 코드
        buy = tr.endswith("1002U")
        assert (b["SLL_TYPE"] == "") == buy
        if not buy and tr == self.reject_sell_tr:
            return web.json_response({"rt_cd": "1", "msg_cd": "EGW00206", "msg1": "TR 코드가 유효하지 않습니다"})
        if b["PDNO"] == "NOPE":
            return web.json_response({"rt_cd": "1", "msg_cd": "APBK0656", "msg1": "주문가능금액이 부족합니다"})
        self.orders.append((tr, b))
        return web.json_response({"rt_cd": "0", "msg1": "ok", "output": {
            "KRX_FWDG_ORD_ORGNO": "01790", "ODNO": f"00301{len(self.orders):05d}", "ORD_TMD": "093015"}})

    async def cancel(self, req):
        self.hit(req, ("VTTT1004U", "TTTT1004U"))
        self.cancels.append(await req.json())
        return web.json_response({"rt_cd": "0", "output": {}})

    async def ccnl(self, req):
        self.hit(req, ("VTTS3035R", "TTTS3035R"))
        q = req.query
        if self.env == "mock":
            assert q["PDNO"] == "" and q["OVRS_EXCG_CD"] == "" and q["SLL_BUY_DVSN"] == "00" and q["ODNO"] == ""
        else:
            assert q["OVRS_EXCG_CD"] == "NASD" and q["ODNO"] == ""
        today = US.now().date().strftime("%Y%m%d")
        assert q["ORD_END_DT"] == today                                # 현지(뉴욕) 날짜 기준
        rows = [
            {"odno": "0030100001", "pdno": "NVDA", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "10", "ft_ccld_qty": "6",
             "ft_ccld_unpr3": "166.21000000", "ft_ccld_amt3": "997.26", "nccs_qty": "4", "prcs_stat_name": "",
             "rjct_rson": "", "rvse_cncl_dvsn": "00", "ord_gno_brno": "01790"},
            {"odno": "0030100777", "pdno": "NVDA", "sll_buy_dvsn_cd": "01", "ft_ord_qty": "7", "ft_ccld_qty": "0",
             "ft_ccld_unpr3": "0", "nccs_qty": "7", "prcs_stat_name": "", "rjct_rson": "", "rvse_cncl_dvsn": "00",
             "ord_gno_brno": "01790"},
        ]
        return web.json_response({"rt_cd": "0", "output": rows})

    async def balance(self, req):
        self.hit(req, ("VTTS3012R", "TTTS3012R"))
        ex = req.query["OVRS_EXCG_CD"]
        assert req.query["TR_CRCY_CD"] == "USD"
        rows = {"NASD": [{"ovrs_pdno": "NVDA", "ovrs_cblc_qty": "12", "pchs_avg_pric": "160.5000"}],
                "AMEX": [{"ovrs_pdno": "SPY", "ovrs_cblc_qty": "3", "pchs_avg_pric": "671.25"}]}.get(ex, [])
        return web.json_response({"rt_cd": "0", "output1": rows, "output2": {}})

    async def psamount(self, req):
        self.hit(req, ("VTTS3007R", "TTTS3007R"))
        if not self.psamount_ok:
            return web.json_response({"rt_cd": "1", "msg_cd": "X", "msg1": "모의투자 미지원"})
        assert req.query["ITEM_CD"] and req.query["OVRS_EXCG_CD"] in ("NASD", "NYSE", "AMEX")
        return web.json_response({"rt_cd": "0", "output": {"ord_psbl_frcr_amt": "25000.50", "ovrs_ord_psbl_amt": "1"}})

    async def present(self, req):
        self.hit(req, ("VTRP6504R", "CTRP6504R"))
        assert req.query["NATN_CD"] == "840" and req.query["WCRC_FRCR_DVSN_CD"] == "02"
        return web.json_response({"rt_cd": "0", "output1": [], "output2": [],
                                  "output3": {"frcr_use_psbl_amt": "18000.00", "tot_asst_amt": "1"}})

    async def price(self, req):
        self.hit(req, ("HHDFS00000300",))
        ex, sym = req.query["EXCD"], req.query["SYMB"]
        assert ex in ("NAS", "NYS", "AMS")                             # 시세는 3글자 코드
        home = {"NVDA": "NAS", "SPY": "AMS", "IBM": "NYS"}.get(sym)
        last = {"NVDA": "166.19", "SPY": "671.00", "IBM": "301.10"}.get(sym, "") if ex == home else ""
        self.tv = getattr(self, "tv", 1000) + 500
        return web.json_response({"rt_cd": "0", "output": {"rsym": "D" + ex + sym, "last": last, "base": "170.00",
                                                          "tvol": str(self.tv)}})

    async def daily(self, req):
        self.hit(req, ("HHDFS76240000",))
        assert req.query["GUBN"] == "0" and req.query["MODP"] == "1"
        bymd = req.query["BYMD"]
        days = [f"202601{d:02d}" for d in (9, 8, 7, 6, 5)] if not bymd else (
            ["20260105", "20260102", "20251231"] if bymd == "20260105" else [bymd])
        rows = [{"xymd": d, "open": "100.0", "high": "110.5", "low": "95.25", "clos": str(100 + int(d[-2:])),
                 "tvol": "1234567"} for d in days]
        return web.json_response({"rt_cd": "0", "output1": {"nrec": str(len(rows))}, "output2": rows})

    async def news(self, req):
        self.hit(req, ("HHPSTH60100C1",))
        assert req.query["NATION_CD"] == "US"
        return web.json_response({"rt_cd": "0", "outblock1": [
            {"news_key": "1", "data_dt": "20260105", "data_tm": "93000", "title": "엔비디아, 실적 가이던스 하향", "symb": "NVDA",
             "source": "연합"}, {"news_key": "2", "title": "", "symb": ""}]})

    async def ws(self, req):
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(req)
        sent = False
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            inp = d["body"]["input"]
            self.subs.append((inp["tr_id"], inp["tr_key"], d["header"]["tr_type"]))
            out = {"iv": IV, "key": KEY} if inp["tr_id"].startswith("H0GSCNI") else {}
            await ws.send_str(json.dumps({"header": {"tr_id": inp["tr_id"], "tr_key": inp["tr_key"]},
                                          "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS", "output": out}}))
            if len(self.subs) >= 3 and not sent:
                sent = True
                two = trade_record("NVDA", "093015", "166.2100", "166.2000", "166.2200", "30", "5000") + "^" + \
                    trade_record("SPY", "093016", "671.0500", "671.0400", "671.0600", "10", "9000", key="DAMS")
                await ws.send_str("0|HDFSCNT0|002|" + two)
                await ws.send_str("0|HDFSCNT0|001|" + trade_record("NVDA", "080000", "150.0", "0", "0", "1", "1", mtyp="2"))
                await ws.send_str("0|HDFSCNT0|001|" + trade_record("NVDA", "093020", "166.3000", "166.29", "166.31", "5",
                                                                   "5005", with_rsym=False))
                await ws.send_str("0|HDFSCNT0|001|" + "^".join(["x"] * 9))        # 형식이 다른 프레임
                n = [""] * 25
                n[2], n[7], n[8], n[9], n[11], n[12] = "0030100001", "NVDA", "6", "001662100", "0", "2"
                await ws.send_str("1|H0GSCNI0|001|" + encrypt("^".join(n)))
                n[2], n[11], n[12] = "0030100999", "1", "1"
                await ws.send_str("1|H0GSCNI0|001|" + encrypt("^".join(n)))
        return ws


def os_cfg(tmp_path, env="mock") -> Config:
    cfg = Config.for_market("US")
    cfg.mode = "kis-live" if env == "real" else "kis-mock"
    cfg.data_dir = str(tmp_path)
    cfg.universe = ["NVDA", "SPY"]
    cfg.kis.exchanges = {}                                    # 거래소 자동 탐색을 확인하기 위해 비운다
    k = cfg.kis
    k.env, k.app_key, k.app_secret, k.account, k.hts_id = env, "AK", "SEC", "12345678", "HTS" if env == "real" else ""
    return cfg


@pytest.fixture
async def fake(unused_tcp_port):
    servers = []

    async def make(env="mock"):
        f = FakeOS(env)
        runner = web.AppRunner(f.app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        f.http, f.wsurl = f"http://127.0.0.1:{unused_tcp_port}", f"ws://127.0.0.1:{unused_tcp_port}"
        servers.append(runner)
        return f
    yield make
    for r in servers:
        await r.cleanup()


async def test_overseas_orders_market_emulation_and_mock_sell_tr(fake, tmp_path):
    f = await fake("mock")
    br = KISOverseasBroker(os_cfg(tmp_path), f.http, f.wsurl)
    br.rl.interval = 0.0
    await br.start()
    try:
        await br.subscribe(["NVDA", "SPY"])
        assert br.exch == {"NVDA": "NAS", "SPY": "AMS"}                  # 현재가가 나오는 거래소를 찾아 기억
        assert json.loads((tmp_path / "kis-mock" / "kis_exchanges.json").read_text()) == br.exch
        o = Order(1, "NVDA", BUY, 10, 166.23, "entry", {}, 0.0)
        await br.place_order(o)
        tr, b = f.orders[0]
        assert tr == "VTTT1002U" and b["OVRS_EXCG_CD"] == "NASD" and b["OVRS_ORD_UNPR"] == "166.23" and b["ORD_QTY"] == "10"
        assert o.broker_id == "30100001"
        # 시장가(limit 0) 매도 → 미국은 시장가가 없으므로 '매수호가보다 1% 불리한 지정가'로 나간다
        br.last["SPY"] = Tick("SPY", 0.0, 671.05, 1, 671.04, 671.06)
        f.reject_sell_tr = "VTTT1001U"                                   # 문서상 TR이 거부되는 서버라고 가정
        s = Order(2, "SPY", SELL, 3, 0.0, "stop_loss", {}, 0.0)
        await br.place_order(s)
        tr, b = f.orders[1]
        assert tr == "VTTT1006U" and b["OVRS_EXCG_CD"] == "AMEX" and b["SLL_TYPE"] == "00"
        assert b["OVRS_ORD_UNPR"] == "664.32"                            # 671.04 × 0.99 = 664.3296 → 내림
        await br.place_order(Order(3, "SPY", SELL, 1, 670.0, "take_profit", {}, 0.0))
        assert f.orders[2][0] == "VTTT1006U"                             # 통한 TR을 기억해 다시 헤매지 않는다
        with pytest.raises(BrokerError, match="거래소를 찾지 못했습니다"):
            await br.place_order(Order(4, "NOPE", BUY, 1, 10.0, "entry", {}, 0.0))     # 어느 거래소에도 없는 종목
        br.exch["NOPE"] = "NAS"
        with pytest.raises(BrokerError, match="주문가능금액"):
            await br.place_order(Order(4, "NOPE", BUY, 1, 10.0, "entry", {}, 0.0))     # 증권사가 거부
        with pytest.raises(BrokerError, match="현재가"):
            br.exch["IBM"] = "NYS"
            await br.place_order(Order(5, "IBM", SELL, 1, 0.0, "stop_loss", {}, 0.0))   # 호가를 모르면 시장가 대체 불가
        # 체결 조회: 주문번호로 검색할 수 없어 목록에서 골라낸다. 가격은 소수 그대로.
        assert await br.poll_order(o) == (6, 166.21, False)
        o.filled = 6
        await br.cancel_order(o)
        c = f.cancels[0]
        assert c["ORGN_ODNO"] == "0030100001" and c["RVSE_CNCL_DVSN_CD"] == "02" and c["ORD_QTY"] == "4" and c["OVRS_ORD_UNPR"] == "0"
        # 응답 유실 주문 찾기: 같은 종목·방향·수량의 '모르는' 주문
        lost = Order(6, "NVDA", SELL, 7, 0.0, "stop_loss", {}, 0.0)
        assert await br.lookup_order(lost) == ("found", "30100777")
        assert await br.lookup_order(Order(7, "NVDA", SELL, 99, 0.0, "x", {}, 0.0)) == ("absent", "")
    finally:
        await br.stop()


async def test_overseas_account_bars_and_calendar(fake, tmp_path):
    f = await fake("mock")
    br = KISOverseasBroker(os_cfg(tmp_path), f.http, f.wsurl)
    br.rl.interval = 0.0
    await br.start()
    try:
        await br.subscribe(["NVDA", "SPY"])
        acct = await br.get_account()
        assert acct.positions == {"NVDA": (12, 160.5), "SPY": (3, 671.25)} and acct.cash == 25000.5
        assert f.calls["/uapi/overseas-stock/v1/trading/inquire-balance"] == 3      # 모의는 거래소별로 따로 조회
        f.psamount_ok = False                                                       # 매수가능금액 조회가 안 되는 서버
        assert (await br.get_account()).cash == 18000.0                             # 현재잔고로 대체
        bars = await br.get_daily_bars("NVDA", 7)
        assert [US.local(b.ts).strftime("%m%d %H:%M") for b in bars] == [
            "1231 09:30", "0102 09:30", "0105 09:30", "0106 09:30", "0107 09:30", "0108 09:30", "0109 09:30"]
        assert bars[-1].close == 109.0 and bars[-1].high == 110.5 and bars[-1].volume == 1234567
        assert await br.get_news() == []                                            # 뉴스는 실전 전용
        assert not await br.is_trading_day(date(2026, 11, 26)) and await br.is_trading_day(date(2026, 11, 27))
        assert not is_trading_day(US, date(2026, 7, 3)) and not is_trading_day(US, date(2027, 1, 18))
    finally:
        await br.stop()


async def test_overseas_mock_polls_quotes_in_session_only(fake, tmp_path):
    f = await fake("mock")
    br = KISOverseasBroker(os_cfg(tmp_path), f.http, f.wsurl)
    br.rl.interval, br.poll_gap = 0.0, 0.01
    ticks = []
    br.on_tick = ticks.append
    await br.start()
    await br.subscribe(["NVDA", "SPY"])
    n0 = f.calls["/uapi/overseas-price/v1/quotations/price"]
    task = asyncio.create_task(br.run())
    try:
        await asyncio.sleep(0.1)
        assert not ticks and f.calls["/uapi/overseas-price/v1/quotations/price"] == n0   # 장 시간이 아니면 조회하지 않는다
        br.expect_data = True
        for _ in range(100):
            if len(ticks) >= 4:
                break
            await asyncio.sleep(0.02)
        assert {t.symbol for t in ticks} == {"NVDA", "SPY"} and ticks[0].price in (166.19, 671.0)
        assert ticks[0].volume == 0 and ticks[2].volume > 0 and ticks[2].cum_volume > ticks[0].cum_volume
    finally:
        await br.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_overseas_real_websocket_ticks_and_notice_pokes(fake, tmp_path):
    f = await fake("real")
    br = KISOverseasBroker(os_cfg(tmp_path, "real"), f.http, f.wsurl)
    br.rl.interval = 0.0
    ticks, pokes, rejects, status = [], [], [], []
    br.on_tick, br.on_poke = ticks.append, pokes.append
    br.on_reject = lambda *a: rejects.append(a)
    br.on_status = status.append
    await br.start()
    await br.subscribe(["NVDA", "SPY"])
    task = asyncio.create_task(br.run())
    try:
        for _ in range(100):
            if len(ticks) >= 3 and pokes and rejects:
                break
            await asyncio.sleep(0.05)
        assert f.subs == [("H0GSCNI0", "HTS", "1"), ("HDFSCNT0", "DNASNVDA", "1"), ("HDFSCNT0", "DAMSSPY", "1")]
        assert len(ticks) == 3                                           # 장전(2) 틱과 형식이 다른 프레임은 버린다
        t = ticks[0]
        assert (t.symbol, t.price, t.bid, t.ask, t.volume, t.cum_volume) == ("NVDA", 166.21, 166.2, 166.22, 30, 5000)
        assert t.ts == US.epoch(date(2026, 1, 5), 9, 30) + 15            # 현지(뉴욕) 일자·시각으로 해석
        assert ticks[1].symbol == "SPY" and ticks[2].price == 166.3      # 25필드 변형도 읽는다
        assert pokes == ["30100001"] and rejects == [("30100999", "주문 거부")]   # 통보는 '조회 신호'로만 쓴다
        assert any("형식" in m for m in status)
        news = await br.get_news()
        assert news and news[0]["symbols"] == ["NVDA"] and news[0]["stale"] is True
    finally:
        await br.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
