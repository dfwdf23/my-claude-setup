"""주문 보조 모드(사람이 체결)와 대시보드의 주문 제안·차트·출처 검사 검증 — 시세 서버는 가짜."""
import asyncio
import time
from datetime import date

import aiohttp
import pytest
from aiohttp import web

from aitrader.brokers.manual import ManualBroker
from aitrader.brokers.sim import PaperBroker
from aitrader.config import Config
from aitrader.dashboard.server import Dashboard
from aitrader.market import US
from aitrader.models import BUY, SELL, OrderIntent, Tick
from aitrader.supervisor import Supervisor

DAY = date(2026, 1, 5)                                    # 월요일
T0 = US.epoch(DAY, 10, 0)                                 # 뉴욕 10:00 (장중)


class FakeYahoo:
    """야후 조회 주소의 응답 형식을 흉내 낸다(spark: 현재가·누적 거래량, chart: 일봉)."""

    def __init__(self):
        self.quotes = {"SPY": (500.5, T0, 1_000_000), "QQQ": (430.25, T0, 500_000)}
        self.hits = 0
        self.agents = []
        app = web.Application()
        app.router.add_get("/v7/finance/spark", self.spark)
        app.router.add_get("/v8/finance/chart/{symbol}", self.chart)
        self.app = app

    async def spark(self, req):
        self.hits += 1
        self.agents.append(req.headers.get("User-Agent", ""))
        assert req.query["range"] == "1d" and req.query["interval"] == "1m"
        res = []
        for sym in req.query["symbols"].split(","):
            if sym in self.quotes:
                px, ts, vol = self.quotes[sym]
                res.append({"symbol": sym, "response": [{"meta": {
                    "symbol": sym, "regularMarketPrice": px, "regularMarketTime": ts, "regularMarketVolume": vol}}]})
            else:
                res.append({"symbol": sym, "response": [{"meta": {"symbol": sym}}]})      # 값이 빠진 종목
        return web.json_response({"spark": {"result": res, "error": None}})

    async def chart(self, req):
        sym = req.match_info["symbol"]
        if sym == "NOPE":
            return web.json_response({"chart": {"result": None, "error": {"code": "Not Found"}}}, status=404)
        if req.query["interval"] == "5m":                                 # 오늘 분봉(마지막은 진행 중인 봉)
            assert req.query["range"] == "1d"
            q = {"open": [500.0, 500.5, 501.0], "high": [500.6, 501.2, 501.1], "low": [499.8, 500.4, 500.9],
                 "close": [500.5, 501.0, 501.0], "volume": [9000, 8000, 100]}
            return web.json_response({"chart": {"result": [{"timestamp": [T0 - 600, T0 - 300, T0],
                                                            "indicators": {"quote": [q]}}]}})
        ts = [US.epoch(date(2026, 1, d), 9, 30) for d in (5, 6, 7, 8)]
        q = {"open": [10.0, 11.0, None, 13.0], "high": [10.5, 11.5, None, 13.5], "low": [9.5, 10.5, None, 12.5],
             "close": [10.2, 11.2, None, 13.2], "volume": [100, 200, None, 400]}
        return web.json_response({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}})


async def serve(app, port):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def manual_cfg(tmp_path) -> Config:
    cfg = Config.for_market("US")
    cfg.mode = "manual"
    cfg.data_dir = str(tmp_path)
    cfg.llm.enabled = False
    cfg.news.enabled = False
    cfg.manual.poll_sec = 0.05
    return cfg


async def test_manual_feed_quotes_ticks_and_daily_bars(tmp_path, unused_tcp_port):
    fake = FakeYahoo()
    runner = await serve(fake.app, unused_tcp_port)
    cfg = manual_cfg(tmp_path)
    br = ManualBroker(cfg, base_url=f"http://127.0.0.1:{unused_tcp_port}")
    await br.start()
    try:
        got = await br._quotes(["SPY", "QQQ", "ZZZZ"])
        assert got == [("SPY", 500.5, float(T0), 1_000_000), ("QQQ", 430.25, float(T0), 500_000)]   # 값 없는 종목은 건너뜀
        assert "@" not in fake.agents[0] and "aitrader" in fake.agents[0]          # 개인 정보 없는 일반 식별자

        bars = await br.get_daily_bars("SPY", 60)
        assert [b.close for b in bars] == [10.2, 11.2, 13.2]                        # 빈 행(휴장·결측)은 제외
        assert bars[0].ts == US.epoch(DAY, 9, 30) and bars[-1].volume == 400        # 시각은 그날 장 시작으로 정규화
        assert await br.get_daily_bars("NOPE", 60) == []

        # 장중에 켰을 때: 지나간 분봉을 받아 실시간 차트를 채운다(진행 중인 봉은 틱으로 직접 만든다)
        assert [b.ts for b in await br.get_intraday_bars("SPY", 300)] == [T0 - 600, T0 - 300, T0]
        assert await br.get_intraday_bars("SPY", 777) == []                         # 지원하지 않는 간격
        sup = Supervisor(cfg, br)
        await sup._backfill_view(["SPY", "NOPE"], T0 + 30)
        assert [b[0] for b in sup.engine.sym["SPY"].view] == [T0 - 600, T0 - 300]
        assert sup.chart("SPY")["view"][-1][1:] == [500.5, 501.2, 500.4, 501.0, 8000]
        assert not sup.engine.sym["NOPE"].view                                      # 실패한 종목은 비워 둔다
        sup.journal.close()

        ticks = []
        br.on_tick = ticks.append
        await br.subscribe(["SPY", "QQQ"])
        br.expect_data = True
        t_start = time.time()
        fake.quotes["SPY"] = (500.5, t_start - 3, 1_000_000)                        # 방금 체결된 시세
        fake.quotes["QQQ"] = (430.25, t_start - 3600, 500_000)                      # 한 시간 전 시세(지연·거래정지)
        task = asyncio.create_task(br.run())
        await asyncio.sleep(0.12)
        fake.quotes["SPY"] = (501.0, time.time() - 1, 1_000_700)                    # 다음 조회에서 거래량이 700 늘었다
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(t.symbol == "SPY" and t.price == 501.0 for t in ticks):
                break
        br._stop = True
        await asyncio.wait_for(task, 3)
        first = next(t for t in ticks if t.symbol == "SPY")
        assert first.volume == 0 and first.cum_volume == 1_000_000                  # 첫 조회는 증가분을 알 수 없다
        nxt = next(t for t in ticks if t.symbol == "SPY" and t.price == 501.0)
        assert nxt.volume == 700 and nxt.cum_volume == 1_000_700
        assert t_start <= first.ts <= nxt.ts <= time.time()                         # 틱 시각은 조회한 시각
        assert not any(t.symbol == "QQQ" for t in ticks) and br.stale > 0           # 오래된 시세는 판단에 쓰지 않는다
        assert await br.is_trading_day(DAY) and not await br.is_trading_day(date(2026, 1, 1))
    finally:
        await br.stop()
        await runner.cleanup()


def _manual_env(tmp_path):
    cfg = manual_cfg(tmp_path)
    br = ManualBroker(cfg, base_url="http://127.0.0.1:9")          # 시세 서버를 쓰지 않는 테스트
    sup = Supervisor(cfg, br)
    e = sup.engine
    e.on_tick(Tick("SPY", T0, 500.0, 100, 499.99, 500.01))
    br.last["SPY"] = 500.0
    sup.symbols = ["SPY"]
    return cfg, br, sup, e


async def test_manual_ticket_confirm_skip_and_expiry(tmp_path):
    cfg, br, sup, e = _manual_env(tmp_path)
    s = e.sym["SPY"]
    sent = []
    sup.notifier.send = sent.append

    # 1) 매수 제안 → 사람이 다른 가격·일부 수량으로 체결했다고 알림
    e._send(OrderIntent("SPY", BUY, 10, 500.05, "entry", {"stop_dist": 5.0, "atr": 2.0, "score": 0.8}), s)
    await asyncio.sleep(0.01)
    (t,) = br.tickets.values()
    assert t["status"] == "OPEN" and t["side"] == BUY and t["price"] == 500.05 and not t["market"]
    assert t["stop"] == 495.05                                          # 매수 제안에 손절 예정가가 함께 뜬다
    assert "SPY" in e.pending and not e.positions                       # 사람이 알려 주기 전에는 장부에 넣지 않는다
    assert sent and "주문 제안" in sent[0] and "SPY" in sent[0]
    assert [ev for ev in e.events if ev["kind"] == "ticket"]
    snap = sup.snapshot()
    assert snap["tickets"][0]["id"] == t["id"] and snap["tickets"][0]["ttl"] == 300.0 and snap["style"] == "swing"

    assert "확인" in br.confirm(t["id"], 11, 500.0)                      # 제안 수량보다 많게는 받지 않는다
    assert "확인" in br.confirm(t["id"], 6, -1)
    assert "너무 다릅니다" in br.confirm(t["id"], 6, 4995.0) and not e.positions   # 자릿수를 잘못 넣은 가격은 거부
    msg = br.confirm(t["id"], 6, 499.5)
    assert "체결 반영" in msg and t["status"] == "FILLED"
    pos = e.positions["SPY"]
    assert pos.qty == 6 and pos.avg_price == 499.5                      # 실제 체결가·수량으로 장부에 반영
    assert "SPY" not in e.pending and not sup.executor.orders           # 남은 4주는 주문하지 않은 것으로 정리
    assert "이미 처리" in br.confirm(t["id"]) and e.positions["SPY"].qty == 6     # 두 번 눌러도 중복 반영 없음
    assert (tmp_path / "manual-us" / "state.json").exists()            # 재시작 대비 장부 저장

    # 2) 매도 제안을 건너뜀 → 포지션 그대로, 같은 종목 제안은 한동안 다시 뜨지 않는다
    e._send(OrderIntent("SPY", SELL, 6, 0.0, "stop_loss"), s)
    await asyncio.sleep(0.01)
    t2 = list(br.tickets.values())[-1]
    assert t2["market"] and t2["side"] == SELL and t2["ref"] == 500.0
    assert "먼저" in sup.manual_sell("SPY", 1, 500.0) and e.positions["SPY"].qty == 6   # 열린 제안이 있으면 그 제안으로 답한다
    assert "건너뜀" in br.skip(t2["id"]) and t2["status"] == "SKIPPED"
    assert e.positions["SPY"].qty == 6 and "SPY" not in e.pending
    assert s.retry_after == e.now + cfg.manual.snooze_min * 60.0
    assert not e.risk.halted                                            # 건너뛰기는 '거부 반복' 정지 사유가 아니다

    # 3) 응답 없는 제안은 실제 시계 기준 5분 뒤 만료된다(시세 시각이 과거여도 뜨자마자 만료되지 않는다)
    s.retry_after = 0.0
    e._send(OrderIntent("SPY", SELL, 6, 0.0, "trailing_stop"), s)
    await asyncio.sleep(0.01)
    t3 = list(br.tickets.values())[-1]
    o = next(iter(sup.executor.orders.values()))
    assert abs(o.created_ts - time.time()) < 5                          # 틱 시각(2026-01-05)이 아니라 벽시계
    sup.executor.on_timer(sup.executor.clock())
    await asyncio.sleep(0.01)
    assert t3["status"] == "OPEN"
    sup.executor.on_timer(sup.executor.clock() + cfg.manual.ticket_ttl_min * 60 + 1)
    await asyncio.sleep(0.01)
    assert t3["status"] == "EXPIRED" and not sup.executor.orders and "SPY" not in e.pending
    assert "이미 처리" in br.confirm(t3["id"]) and e.positions["SPY"].qty == 6    # 만료된 제안은 체결로 못 바꾼다

    # 4) 매도 체결 → 거래 종료, 손익이 일지에 남는다
    s.retry_after = 0.0
    e._send(OrderIntent("SPY", SELL, 6, 0.0, "take_profit"), s)
    await asyncio.sleep(0.01)
    t4 = list(br.tickets.values())[-1]
    br.confirm(t4["id"], None, 510.0)
    assert not e.positions and len(e.trades) == 1 and e.trades[0].exit_price == 510.0 and e.trades[0].pnl > 0
    sup.journal.close()


async def test_manual_restart_keeps_ledger(tmp_path):
    cfg, br, sup, e = _manual_env(tmp_path)
    e._send(OrderIntent("SPY", BUY, 4, 500.0, "entry", {"stop_dist": 5.0, "atr": 2.0, "score": 0.8}), e.sym["SPY"])
    await asyncio.sleep(0.01)
    br.confirm(next(iter(br.tickets)), 4, 500.0)
    cash = e.cash
    sup.journal.close()

    br2 = ManualBroker(cfg, base_url="http://127.0.0.1:9")
    sup2 = Supervisor(cfg, br2)
    await sup2._restore(None)                                           # 계좌 조회가 없으므로 저장된 장부를 이어받는다
    e2 = sup2.engine
    assert e2.positions["SPY"].qty == 4 and abs(e2.cash - cash) < 1e-6
    assert not br2.tickets                                              # 답하지 않은 제안은 이어받지 않는다(조건이 남아 있으면 다시 뜬다)
    sup2.journal.close()


async def test_dashboard_ticket_chart_and_origin_guard(tmp_path, unused_tcp_port):
    cfg, br, sup, e = _manual_env(tmp_path)
    dash = Dashboard(sup, "127.0.0.1", unused_tcp_port, token="tok")
    await dash.start()
    base = f"http://127.0.0.1:{unused_tcp_port}"
    h = {"X-Token": "tok"}
    try:
        e._send(OrderIntent("SPY", BUY, 10, 500.05, "entry", {"stop_dist": 5.0, "atr": 2.0, "score": 0.8}), e.sym["SPY"])
        await asyncio.sleep(0.01)
        tid = next(iter(br.tickets))
        body = {"id": tid, "action": "fill", "qty": "10", "price": "500.10"}
        async with aiohttp.ClientSession() as s:
            # 다른 사이트가 사용자의 브라우저를 통해 보낸 요청(출처 불일치)은 토큰이 맞아도 거부
            async with s.post(base + "/api/ticket", json=body, headers={**h, "Origin": "http://evil.example"}) as r:
                assert r.status == 403
            async with s.post(base + "/api/control", json={"action": "flatten"},
                              headers={**h, "Origin": "http://evil.example"}) as r:
                assert r.status == 403
            assert not e.risk.halted and not e.positions
            async with s.post(base + "/api/ticket", json=body) as r:
                assert r.status == 401                                  # 토큰 없음
            async with s.post(base + "/api/ticket", data='{"id":"M1","action":"fill"}',
                              headers={**h, "Content-Type": "text/plain"}) as r:
                assert r.status == 415                                  # 폼 전송으로 위장한 요청 차단
            async with s.post(base + "/api/ticket", json={"id": tid, "action": "nope"}, headers=h) as r:
                assert r.status == 400
            async with s.post(base + "/api/ticket", json={"id": tid, "action": "fill", "qty": "abc"}, headers=h) as r:
                assert r.status == 400
            assert not e.positions

            # 같은 출처(대시보드 화면)에서 온 요청은 통과
            async with s.post(base + "/api/ticket", json=body, headers={**h, "Origin": base}) as r:
                assert r.status == 200 and "체결 반영" in (await r.json())["message"]
            assert e.positions["SPY"].qty == 10 and e.positions["SPY"].avg_price == 500.10

            e.sym["SPY"].retry_after = 0.0
            e._send(OrderIntent("SPY", SELL, 10, 0.0, "stop_loss"), e.sym["SPY"])
            await asyncio.sleep(0.01)
            tid2 = list(br.tickets)[-1]
            async with s.post(base + "/api/ticket", json={"id": tid2, "action": "skip"}, headers=h) as r:
                assert r.status == 200
            assert br.tickets[tid2]["status"] == "SKIPPED" and e.positions["SPY"].qty == 10
            cash0 = e.cash

            # 제안 없이 직접 판 것(걸어 둔 손절 주문이 체결된 경우 등)을 장부에 반영
            async with s.post(base + "/api/ticket", json={"id": "SPY", "action": "sold", "qty": "4", "price": "498"},
                              headers={**h, "Origin": "http://evil.example"}) as r:
                assert r.status == 403
            async with s.post(base + "/api/ticket", json={"id": "SPY", "action": "sold", "qty": "4"}, headers=h) as r:
                assert r.status == 400                                  # 가격 없이는 받지 않는다
            async with s.post(base + "/api/ticket", json={"id": "SPY", "action": "sold", "qty": "99", "price": "498"},
                              headers=h) as r:
                assert "확인" in (await r.json())["message"] and e.positions["SPY"].qty == 10
            async with s.post(base + "/api/ticket", json={"id": "SPY", "action": "sold", "qty": "4", "price": "49.8"},
                              headers=h) as r:
                assert "너무 다릅니다" in (await r.json())["message"] and e.positions["SPY"].qty == 10
            async with s.post(base + "/api/ticket", json={"id": "NOPE", "action": "sold", "qty": "1", "price": "5"},
                              headers=h) as r:
                assert "없습니다" in (await r.json())["message"]
            async with s.post(base + "/api/ticket", json={"id": "spy", "action": "sold", "qty": "4", "price": "498"},
                              headers=h) as r:
                assert r.status == 200 and "직접 매도 반영" in (await r.json())["message"]
            assert e.positions["SPY"].qty == 6 and abs(e.cash - cash0 - 4 * 498 * (1 - cfg.fees.commission - cfg.fees.sell_tax)) < 0.01
            e.positions["SPY"].qty += 4                                 # 아래 차트 검사를 위해 수량만 되돌린다

            # 차트 자료
            async with s.get(base + "/api/chart?symbol=SPY") as r:
                assert r.status == 401
            async with s.get(base + "/api/chart?symbol=SPY", headers=h) as r:
                c = await r.json()
                assert r.status == 200 and c["symbol"] == "SPY" and c["last"] == 500.0
                assert c["levels"]["avg"] == 500.10 and c["marks"][0]["side"] == "BUY"
                assert c["cur"][1:5] == [500.0, 500.0, 500.0, 500.0] and c["bar_sec"] == 86_400
            async with s.get(base + "/api/chart?symbol=NOPE", headers=h) as r:
                assert r.status == 404

            # 웹소켓: 다른 출처는 거부, 같은 출처는 종목을 고르면 차트가 함께 온다
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await s.ws_connect(base + "/ws?token=tok", headers={"Origin": "http://evil.example"})
            assert err.value.status == 403
            async with s.ws_connect(base + "/ws?token=tok", headers={"Origin": base}) as ws:
                first = await ws.receive_json(timeout=3)
                assert "chart" not in first and first["market"]["currency"] == "USD"
                assert first["tickets"][0]["status"] == "SKIPPED" and first["news_on"] is False
                await ws.send_json({"sel": "SPY"})
                for _ in range(5):
                    msg = await ws.receive_json(timeout=3)
                    if "chart" in msg:
                        break
                assert msg["chart"]["symbol"] == "SPY"
                await ws.send_json({"sel": "NOPE"})                     # 모르는 종목은 차트 없음(오류 아님)
                for _ in range(5):
                    msg = await ws.receive_json(timeout=3)
                    if msg.get("chart") is None:
                        break
                assert msg["chart"] is None
    finally:
        await dash.stop()
        sup.journal.close()


async def test_ticket_api_is_manual_mode_only(tmp_path, unused_tcp_port):
    cfg = Config.for_market("US")
    cfg.data_dir = str(tmp_path)
    cfg.llm.enabled = False
    sup = Supervisor(cfg, PaperBroker(cfg))
    dash = Dashboard(sup, "127.0.0.1", unused_tcp_port)
    await dash.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{unused_tcp_port}/api/ticket", json={"id": "M1", "action": "fill"}) as r:
                assert r.status == 400 and (await r.json())["error"] == "manual mode only"
        assert "주문 보조 모드" in sup.manual_sell("SPY", 1, 100.0)        # 자동 주문 모드에서는 장부를 손으로 고칠 수 없다
    finally:
        await dash.stop()
        sup.journal.close()
