"""KIS 연동 검증 — 공식 예제의 요청/응답 형식을 흉내 낸 가짜 서버를 상대로 돌린다.

실제 한국투자증권 서버를 호출하지 않는다. 형식 해석과 흐름(토큰·주문·취소·조회·웹소켓·복호화)만 확인한다.
"""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from aitrader.brokers.base import BrokerError
from aitrader.brokers.kis import KISBroker
from aitrader.config import Config
from aitrader.models import BUY, SELL, Order

KEY, IV = "k" * 32, "i" * 16
KST = timezone(timedelta(hours=9))


def encrypt(plain: str) -> str:
    p = padding.PKCS7(128).padder()
    data = p.update(plain.encode()) + p.finalize()
    e = Cipher(algorithms.AES(KEY.encode()), modes.CBC(IV.encode())).encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()


def tick_record(code, hhmmss, price, ask, bid, vol, hour_cls="0") -> str:
    f = ["0"] * 46
    f[0], f[1], f[2], f[10], f[11], f[12], f[33], f[43] = code, hhmmss, price, ask, bid, vol, "20260105", hour_cls
    return "^".join(f)


def notice(odno, qty, price, refused="0", filled="2") -> str:
    f = [""] * 26
    f[0], f[1], f[2], f[4], f[8], f[9], f[10], f[11], f[12], f[13] = \
        "HTS", "1234567801", odno, "02", "069500", qty, price, "093020", refused, filled
    return "^".join(f)


class FakeKIS:
    def __init__(self):
        self.token_calls = 0
        self.orders = []
        self.cancels = []
        self.subs = []
        self.pongs = 0
        self.expire_once = False
        app = web.Application()
        app.add_routes([
            web.post("/oauth2/tokenP", self.token), web.post("/oauth2/Approval", self.approval),
            web.post("/uapi/domestic-stock/v1/trading/order-cash", self.order),
            web.post("/uapi/domestic-stock/v1/trading/order-rvsecncl", self.cancel),
            web.get("/uapi/domestic-stock/v1/trading/inquire-balance", self.balance),
            web.get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self.ccld),
            web.get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", self.daily),
            web.get("/tryitout", self.ws),
        ])
        self.app = app

    async def token(self, req):
        body = await req.json()
        assert body == {"grant_type": "client_credentials", "appkey": "AK", "appsecret": "SEC"}
        self.token_calls += 1
        return web.json_response({"access_token": f"T{self.token_calls}", "expires_in": 86400})

    async def approval(self, req):
        body = await req.json()
        assert body["secretkey"] == "SEC"
        return web.json_response({"approval_key": "APPROVAL"})

    def _auth(self, req, tr_ids):
        assert req.headers["authorization"].startswith("Bearer T")
        assert req.headers["appkey"] == "AK" and req.headers["custtype"] == "P"
        assert req.headers["tr_id"] in tr_ids, req.headers["tr_id"]

    async def order(self, req):
        self._auth(req, ("VTTC0012U", "VTTC0011U"))
        b = await req.json()
        if self.expire_once:
            self.expire_once = False
            return web.json_response({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."})
        assert set(b) == {"CANO", "ACNT_PRDT_CD", "PDNO", "ORD_DVSN", "ORD_QTY", "ORD_UNPR",
                          "EXCG_ID_DVSN_CD", "SLL_TYPE", "CNDT_PRIC"}
        assert all(isinstance(v, str) for v in b.values())                 # 수량·단가는 문자열이어야 한다
        assert (req.headers["tr_id"] == "VTTC0012U") == (b["SLL_TYPE"] == "")
        if b["PDNO"] == "999999":
            return web.json_response({"rt_cd": "1", "msg_cd": "APBK0952", "msg1": "주문가능금액을 초과 했습니다"})
        self.orders.append(b)
        return web.json_response({"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료",
                                  "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000012345", "ORD_TMD": "093015"}})

    async def cancel(self, req):
        self._auth(req, ("VTTC0013U",))
        self.cancels.append(await req.json())
        return web.json_response({"rt_cd": "0", "msg1": "ok", "output": {}})

    async def balance(self, req):
        self._auth(req, ("VTTC8434R",))
        assert req.query["CANO"] == "12345678" and req.query["ACNT_PRDT_CD"] == "01"
        return web.json_response({"rt_cd": "0", "output1": [
            {"pdno": "069500", "hldg_qty": "10", "pchs_avg_pric": "35000.0000"},
            {"pdno": "000001", "hldg_qty": "0", "pchs_avg_pric": "0"}],
            "output2": [{"dnca_tot_amt": "5000000", "prvs_rcdl_excc_amt": "4650000"}]})

    async def ccld(self, req):
        self._auth(req, ("VTTC0081R",))
        return web.json_response({"rt_cd": "0", "output1": [
            {"odno": "0000099999", "tot_ccld_qty": "1", "avg_prvs": "1", "rmn_qty": "0", "cncl_yn": "N"},
            {"odno": "0000012345", "tot_ccld_qty": "7", "avg_prvs": "35010", "rmn_qty": "3", "cncl_yn": "N",
             "rjct_qty": "0"}]})

    async def daily(self, req):
        assert req.headers["tr_id"] == "FHKST03010100" and req.query["FID_PERIOD_DIV_CODE"] == "D"
        rows = [{"stck_bsop_date": f"202601{d:02d}", "stck_oprc": "100", "stck_hgpr": "110", "stck_lwpr": "90",
                 "stck_clpr": str(100 + d), "acml_vol": "1000"} for d in (9, 8, 7, 6, 5)]
        return web.json_response({"rt_cd": "0", "output2": rows + [{}]})

    async def ws(self, req):
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(req)
        sent = False
        async for msg in ws:
            if msg.type == WSMsgType.PONG:
                self.pongs += 1
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            assert d["header"]["approval_key"] == "APPROVAL" and d["header"]["custtype"] == "P"
            inp = d["body"]["input"]
            self.subs.append((inp["tr_id"], inp["tr_key"], d["header"]["tr_type"]))
            out = {"iv": IV, "key": KEY} if inp["tr_id"] == "H0STCNI9" else {}
            await ws.send_str(json.dumps({"header": {"tr_id": inp["tr_id"], "tr_key": inp["tr_key"], "encrypt": "N"},
                                          "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS",
                                                   "output": out}}))
            if len(self.subs) >= 3 and not sent:
                sent = True
                two = tick_record("069500", "093015", "35010", "35015", "35010", "12") + "^" + \
                    tick_record("122630", "093016", "18200", "18205", "18200", "3")
                await ws.send_str("0|H0STCNT0|002|" + two)
                await ws.send_str("0|H0STCNT0|001|" + tick_record("069500", "085900", "34000", "0", "0", "0", "B"))
                await ws.send_str(json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260105093017"}}))
                await ws.send_str("1|H0STCNI9|001|" + encrypt(notice("0000012345", "7", "35010")))
                await ws.send_str("1|H0STCNI9|001|" + encrypt(notice("0000012345", "0", "0", filled="1")))   # 접수 통보
                await ws.send_str("1|H0STCNI9|001|" + encrypt(notice("0000077777", "0", "0", refused="1")))
        return ws


def kis_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.mode = "kis-mock"
    cfg.data_dir = str(tmp_path)
    k = cfg.kis
    k.env, k.app_key, k.app_secret, k.account, k.hts_id = "mock", "AK", "SEC", "12345678", "HTS"
    return cfg


@pytest.fixture
async def fake(unused_tcp_port):
    f = FakeKIS()
    runner = web.AppRunner(f.app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    f.http = f"http://127.0.0.1:{unused_tcp_port}"
    f.wsurl = f"ws://127.0.0.1:{unused_tcp_port}"
    yield f
    await runner.cleanup()


async def test_kis_rest_flow(fake, tmp_path):
    cfg = kis_cfg(tmp_path)
    br = KISBroker(cfg, fake.http, fake.wsurl)
    br.rl.interval = 0.0
    await br.start()
    try:
        assert fake.token_calls == 1
        o = Order(1, "069500", BUY, 10, 35015.0, "entry", {}, 0.0)
        await br.place_order(o)
        assert o.broker_id == "12345" and o.broker_org == "00950"            # 주문번호 앞자리 0 제거
        assert fake.orders[0] == {"CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "069500", "ORD_DVSN": "00",
                                  "ORD_QTY": "10", "ORD_UNPR": "35015", "EXCG_ID_DVSN_CD": "KRX",
                                  "SLL_TYPE": "", "CNDT_PRIC": ""}
        s = Order(2, "069500", SELL, 10, 0.0, "stop_loss", {}, 0.0)           # 시장가 매도
        await br.place_order(s)
        assert fake.orders[1]["ORD_DVSN"] == "01" and fake.orders[1]["ORD_UNPR"] == "0" and fake.orders[1]["SLL_TYPE"] == "01"
        assert await br.poll_order(o) == (7, 35010.0, False)
        await br.cancel_order(o)
        c = fake.cancels[0]
        assert c["ORGN_ODNO"] == "0000012345" and c["KRX_FWDG_ORD_ORGNO"] == "00950" and c["RVSE_CNCL_DVSN_CD"] == "02"
        acct = await br.get_account()
        assert acct.cash == 4_650_000 and acct.positions == {"069500": (10, 35000.0)}
        bars = await br.get_daily_bars("069500", 3)
        assert [b.close for b in bars] == [107.0, 108.0, 109.0]              # 오래된 것 → 최신 순
        with pytest.raises(BrokerError, match="주문가능금액"):
            await br.place_order(Order(3, "999999", BUY, 1, 1000.0, "entry", {}, 0.0))
        fake.expire_once = True                                             # 토큰 만료 → 재발급 후 재시도
        await br.place_order(Order(4, "069500", BUY, 1, 35015.0, "entry", {}, 0.0))
        assert fake.token_calls == 2 and len(fake.orders) == 3
    finally:
        await br.stop()
    br2 = KISBroker(cfg, fake.http, fake.wsurl)                              # 재시작 시 토큰 캐시 재사용
    await br2.start()
    await br2.stop()
    assert fake.token_calls == 2


async def test_kis_websocket_ticks_and_encrypted_notice(fake, tmp_path):
    br = KISBroker(kis_cfg(tmp_path), fake.http, fake.wsurl)
    ticks, fills, rejects = [], [], []
    br.on_tick = ticks.append
    br.on_fill = lambda *a: fills.append(a)
    br.on_reject = lambda *a: rejects.append(a)
    await br.start()
    await br.subscribe(["069500", "122630"])
    task = asyncio.create_task(br.run())
    try:
        for _ in range(100):
            if len(ticks) >= 2 and fills and rejects and fake.pongs:
                break
            await asyncio.sleep(0.05)
        assert fake.subs == [("H0STCNI9", "HTS", "1"), ("H0STCNT0", "069500", "1"), ("H0STCNT0", "122630", "1")]
        assert len(ticks) == 2                                              # 예상체결(B)은 걸러진다
        t = ticks[0]
        want = datetime(2026, 1, 5, 9, 30, 15, tzinfo=KST).timestamp()
        assert (t.symbol, t.ts, t.price, t.volume, t.bid, t.ask) == ("069500", want, 35010.0, 12, 35010.0, 35015.0)
        assert ticks[1].symbol == "122630" and ticks[1].price == 18200.0
        assert len(fills) == 1 and fills[0][:3] == ("12345", 7, 35010.0)    # 복호화 + 체결만(접수 통보 제외)
        assert rejects == [("77777", "주문 거부")]
        assert fake.pongs == 1 and br.ws_connected
    finally:
        await br.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_kis_requires_credentials(tmp_path):
    cfg = kis_cfg(tmp_path)
    cfg.kis.hts_id = ""
    with pytest.raises(SystemExit, match="KIS_HTS_ID"):
        KISBroker(cfg)


async def test_live_supervisor_path_with_fake_kis(fake, tmp_path):
    """실매매 경로 통합: 계좌 연결(운용 한도) → 장전 준비 → 실시간 틱이 엔진까지 → 정지 상태가 재시작 후에도 유지."""
    from datetime import date

    from aitrader.supervisor import Supervisor
    cfg = kis_cfg(tmp_path)
    cfg.capital = 3_000_000                       # 계좌 현금(465만)보다 작은 운용 한도
    cfg.llm.enabled = False
    br = KISBroker(cfg, fake.http, fake.wsurl)
    br.rl.interval = 0.0
    sup = Supervisor(cfg, br)
    await br.start()
    await sup.reconcile(initial=True)
    e = sup.engine
    assert e.cash == 3_000_000 and e.equity == 3_000_000 and not e.positions   # 계좌의 기존 보유분(069500 10주)은 건드리지 않음
    await sup.day_start(date(2026, 1, 5))
    assert sup.phase == "OPEN" and len(sup.symbols) == 8
    task = asyncio.create_task(br.run())
    try:
        for _ in range(100):
            if e.ticks >= 2:
                break
            await asyncio.sleep(0.05)
        assert e.ticks == 2 and e.sym["069500"].last == 35010.0 and e.sym["069500"].etf
        sup.control("halt")
    finally:
        await br.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sup.journal.close()
    br2 = KISBroker(cfg, fake.http, fake.wsurl)
    sup2 = Supervisor(cfg, br2)
    await br2.start()
    await sup2.reconcile(initial=True)
    assert sup2.engine.risk.halted and sup2.engine.risk.halt_reason == "수동 정지"   # 재시작해도 정지 유지
    await br2.stop()
    sup2.journal.close()
