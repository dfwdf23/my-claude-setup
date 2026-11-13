"""한국투자증권(KIS) Open API 연동 — REST(주문·조회) + WebSocket(실시간 체결가·체결통보).

스펙 출처: github.com/koreainvestment/open-trading-api (공식 예제) 기준으로 작성.
  · 주문 TR: 실전 매수 TTTC0012U / 매도 TTTC0011U, 모의 VTTC0012U / VTTC0011U
  · 실시간 체결가 H0STCNT0, 체결통보 H0STCNI0(실전) / H0STCNI9(모의, AES-256-CBC 암호화)
주의: 이 모듈은 실제 KIS 서버가 아니라 스펙을 흉내 낸 가짜 서버(tests/)로만 검증했다.
반드시 모의투자(kis-mock)에서 먼저 충분히 돌려 본 뒤 실전에 쓸 것.

속도 관련: 연결 재사용(keep-alive), DNS 캐시, 토큰 캐시, 헤더 사전 구성으로 주문 1건당
추가 왕복을 없앴다. 주문 왕복 시간은 사실상 '서버까지의 네트워크 거리'가 결정하므로
서울 리전 서버에서 실행하는 것이 가장 효과가 크다.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from ..config import Config
from ..models import BUY, AccountSnapshot, Bar, Order, Tick
from .base import Broker, BrokerError

try:
    import orjson
    _loads = orjson.loads
except ImportError:                                    # pragma: no cover
    _loads = json.loads

log = logging.getLogger("aitrader.kis")
KST_TZ = timezone(timedelta(hours=9))
REST = {"real": "https://openapi.koreainvestment.com:9443", "mock": "https://openapivts.koreainvestment.com:29443"}
WS = {"real": "ws://ops.koreainvestment.com:21000", "mock": "ws://ops.koreainvestment.com:31000"}
TR = {
    "real": {"buy": "TTTC0012U", "sell": "TTTC0011U", "cancel": "TTTC0013U", "balance": "TTTC8434R",
             "ccld": "TTTC0081R", "notice": "H0STCNI0"},
    "mock": {"buy": "VTTC0012U", "sell": "VTTC0011U", "cancel": "VTTC0013U", "balance": "VTTC8434R",
             "ccld": "VTTC0081R", "notice": "H0STCNI9"},
}
MAX_SUBS = 40


class RateLimiter:
    """초당 호출 한도(실전 20건/초, 모의 2건/초)를 넘지 않게 간격을 둔다."""

    def __init__(self, per_sec: float) -> None:
        self.interval = 1.0 / per_sec
        self._next = 0.0

    async def wait(self) -> None:
        now = asyncio.get_running_loop().time()
        t = max(now, self._next)
        self._next = t + self.interval
        if t > now:
            await asyncio.sleep(t - now)


def aes_cbc_decrypt(key: str, iv: str, b64: str) -> str:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
    raw = d.update(base64.b64decode(b64)) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return (u.update(raw) + u.finalize()).decode("utf-8")


class KISBroker(Broker):
    name = "kis"
    need_hts = True            # 국내: 체결통보(웹소켓)에 HTS ID 필요
    TR_TABLE = TR

    def __init__(self, cfg: Config, base_url: str = "", ws_url: str = "") -> None:
        super().__init__()
        k = cfg.kis
        self.cfg = cfg
        self.env = "real" if k.env == "real" else "mock"
        need = [("KIS_APP_KEY", k.app_key), ("KIS_APP_SECRET", k.app_secret), ("KIS_ACCOUNT", k.account)]
        if self.need_hts:
            need.append(("KIS_HTS_ID", k.hts_id))
        missing = [n for n, v in need if not v]
        if missing:
            raise SystemExit("한국투자증권 연동에 필요한 환경변수가 없습니다: " + ", ".join(missing)
                             + "\n(.env.example 참고. KIS_HTS_ID 는 체결통보 수신에 필요합니다)")
        self.base = base_url or REST[self.env]
        self.ws_base = ws_url or WS[self.env]
        self.tr = self.TR_TABLE[self.env]
        self.rl = RateLimiter(18.0 if self.env == "real" else 1.8)
        self.session: aiohttp.ClientSession | None = None
        self._tok = ""
        self._tok_exp = 0.0
        self._tok_lock = asyncio.Lock()
        self._tok_file = Path(cfg.data_dir) / cfg.mode / "kis_token.json"
        self._symbols: list[str] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._approval = ""
        self._aes: tuple[str, str] | None = None
        self._aes_by_tr: dict[str, tuple[str, str]] = {}
        self._raw: dict[str, tuple[str, str]] = {}       # 주문번호(정규화) → (주문조직번호, 원주문번호)
        self._day_cache: dict[str, float] = {}
        self._stop = False
        self.ws_connected = False
        self.supports_poll = True
        self.idle_timeout = 45.0                         # 장중 이 시간 동안 아무 메시지도 없으면 죽은 연결로 본다
        self._last_msg = time.monotonic()
        self._approval_ts = 0.0

    # ------------------------------------------------------------------ 수명주기
    async def start(self) -> None:
        conn = aiohttp.TCPConnector(limit=20, ttl_dns_cache=600, keepalive_timeout=75)
        self.session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=10),
                                             json_serialize=json.dumps)
        await self._token()                              # 장 시작 전에 미리 인증·연결 예열

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self.session is not None:
            await self.session.close()

    # ------------------------------------------------------------------ 인증
    async def _token(self) -> str:
        if self._tok and time.time() < self._tok_exp - 600:
            return self._tok
        async with self._tok_lock:
            if self._tok and time.time() < self._tok_exp - 600:
                return self._tok
            try:                                         # 토큰 발급은 횟수 제한이 있어 파일에 캐시한다
                c = json.loads(self._tok_file.read_text())
                if c.get("env") == self.env and c.get("exp", 0) - 600 > time.time():
                    self._tok, self._tok_exp = c["token"], c["exp"]
                    return self._tok
            except (OSError, ValueError, KeyError):
                pass
            k = self.cfg.kis
            async with self.session.post(self.base + "/oauth2/tokenP", json={
                    "grant_type": "client_credentials", "appkey": k.app_key, "appsecret": k.app_secret}) as r:
                d = _loads(await r.read())
            if "access_token" not in d:
                raise BrokerError(f"토큰 발급 실패: {d.get('error_description') or d.get('msg1') or d}")
            self._tok = d["access_token"]
            self._tok_exp = time.time() + float(d.get("expires_in", 86400))
            try:
                self._tok_file.parent.mkdir(parents=True, exist_ok=True)
                self._tok_file.write_text(json.dumps({"env": self.env, "token": self._tok, "exp": self._tok_exp}))
                os.chmod(self._tok_file, 0o600)
            except OSError:
                pass
            return self._tok

    async def _headers(self, tr_id: str, cont: str = "") -> dict:
        k = self.cfg.kis
        return {"content-type": "application/json; charset=utf-8", "authorization": "Bearer " + await self._token(),
                "appkey": k.app_key, "appsecret": k.app_secret, "tr_id": tr_id, "custtype": "P", "tr_cont": cont}

    async def _call(self, method: str, path: str, tr_id: str, payload: dict, cont: str = "",
                    retry: bool = True) -> tuple[dict, str]:
        await self.rl.wait()
        h = await self._headers(tr_id, cont)
        kw = {"params": payload} if method == "GET" else {"data": json.dumps(payload)}
        async with self.session.request(method, self.base + path, headers=h, **kw) as r:
            body = await r.read()
            next_cont = r.headers.get("tr_cont", "")
            status = r.status
        try:
            d = _loads(body)
        except ValueError:
            raise BrokerError(f"HTTP {status}: 응답 해석 실패")
        if d.get("rt_cd") == "0":
            return d, next_cont
        code = d.get("msg_cd", "")
        if retry and code == "EGW00123":                 # 토큰 만료 → 재발급 후 1회 재시도
            self._tok = ""
            try:
                self._tok_file.unlink()
            except OSError:
                pass
            return await self._call(method, path, tr_id, payload, cont, retry=False)
        if retry and code == "EGW00201":                 # 초당 거래건수 초과 → 접수되지 않았으므로 재시도 안전
            await asyncio.sleep(0.6 if self.env == "mock" else 0.1)
            return await self._call(method, path, tr_id, payload, cont, retry=False)
        raise BrokerError(f"{d.get('msg1', '').strip() or 'API 오류'} [{code or status}]")

    # ------------------------------------------------------------------ 주문
    def _acct(self) -> dict:
        k = self.cfg.kis
        return {"CANO": k.account, "ACNT_PRDT_CD": k.product}

    async def place_order(self, order: Order) -> None:
        buy = order.side == BUY
        body = {**self._acct(), "PDNO": order.symbol,
                "ORD_DVSN": "00" if order.limit_price else "01",          # 00 지정가, 01 시장가
                "ORD_QTY": str(order.qty), "ORD_UNPR": str(int(order.limit_price)) if order.limit_price else "0",
                "EXCG_ID_DVSN_CD": self.cfg.kis.exchange, "SLL_TYPE": "" if buy else "01", "CNDT_PRIC": ""}
        d, _ = await self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash",
                                self.tr["buy" if buy else "sell"], body)
        out = d.get("output") or {}
        odno = str(out.get("ODNO", ""))
        if not odno:
            raise BrokerError("주문번호 없음")
        key = odno.lstrip("0") or "0"
        self._raw[key] = (str(out.get("KRX_FWDG_ORD_ORGNO", "")), odno)
        order.broker_id = key
        order.broker_org = self._raw[key][0]

    async def cancel_order(self, order: Order) -> None:
        # 재시작 후에는 원래 주문번호 표기를 모른다 → 10자리로 0을 채운다
        org, odno = self._raw.get(order.broker_id, (order.broker_org, order.broker_id.zfill(10)))
        body = {**self._acct(), "KRX_FWDG_ORD_ORGNO": org, "ORGN_ODNO": odno, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
                "EXCG_ID_DVSN_CD": self.cfg.kis.exchange}
        await self._call("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", self.tr["cancel"], body)

    async def poll_order(self, order: Order) -> tuple[int, float, bool]:
        """체결통보 누락 대비: 주문의 (누적 체결수량, 평균가, 종료 여부)를 조회한다."""
        today = datetime.now(KST_TZ).strftime("%Y%m%d")
        _, odno = self._raw.get(order.broker_id, ("", order.broker_id.zfill(10)))
        q = {**self._acct(), "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00",
             "PDNO": order.symbol, "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00",
             "ORD_GNO_BRNO": "", "ODNO": odno, "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        d, _ = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self.tr["ccld"], q)
        for row in d.get("output1") or []:
            if (str(row.get("odno", "")).lstrip("0") or "0") == order.broker_id:
                filled = int(float(row.get("tot_ccld_qty") or 0))
                done = (row.get("cncl_yn") == "Y" or int(float(row.get("rmn_qty") or 0)) == 0
                        or int(float(row.get("rjct_qty") or 0)) > 0)
                return filled, float(row.get("avg_prvs") or 0.0), done
        return 0, 0.0, False

    async def lookup_order(self, order: Order) -> tuple[str, str]:
        """주문 응답을 못 받았을 때: 오늘 주문 내역에서 같은 종목·방향·수량의 '모르는' 주문을 찾는다."""
        now = datetime.now(KST_TZ)
        today = now.strftime("%Y%m%d")
        sent = datetime.fromtimestamp(order.created_ts or time.time(), KST_TZ) - timedelta(seconds=10)
        since = sent.strftime("%H%M%S") if sent.date() == now.date() else "000000"
        q = {**self._acct(), "INQR_STRT_DT": today, "INQR_END_DT": today,
             "SLL_BUY_DVSN_CD": "02" if order.side == BUY else "01", "PDNO": order.symbol, "CCLD_DVSN": "00",
             "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "",
             "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        try:
            d, _ = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self.tr["ccld"], q)
        except Exception:
            return "unknown", ""
        best = None
        for row in d.get("output1") or []:
            key = str(row.get("odno", "")).lstrip("0") or "0"
            if key in self._raw or row.get("pdno") != order.symbol:
                continue
            if int(float(row.get("ord_qty") or 0)) != order.qty or str(row.get("ord_tmd", "")) < since:
                continue
            if best is None or str(row.get("ord_tmd", "")) > str(best.get("ord_tmd", "")):
                best = row
        if best is None:
            return "absent", ""
        key = str(best["odno"]).lstrip("0") or "0"
        self._raw[key] = (str(best.get("ord_gno_brno", "")), str(best["odno"]))
        order.broker_org = self._raw[key][0]
        return "found", key

    # ------------------------------------------------------------------ 조회
    async def get_account(self) -> AccountSnapshot:
        q = {**self._acct(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
             "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
             "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        pos: dict = {}
        cash = 0.0
        cont = ""
        for _ in range(6):
            d, nxt = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance",
                                      self.tr["balance"], q, cont)
            for row in d.get("output1") or []:
                qty = int(float(row.get("hldg_qty") or 0))
                if qty > 0:
                    pos[row["pdno"]] = (qty, float(row.get("pchs_avg_pric") or 0.0))
            o2 = d.get("output2") or [{}]
            o2 = o2[0] if isinstance(o2, list) else o2
            # D+2 예수금(오늘 매매 반영). 없으면 예수금 총액.
            cash = float(o2.get("prvs_rcdl_excc_amt") or o2.get("dnca_tot_amt") or 0.0)
            if nxt not in ("F", "M"):
                break
            cont = "N"
            q["CTX_AREA_FK100"] = d.get("ctx_area_fk100", "")
            q["CTX_AREA_NK100"] = d.get("ctx_area_nk100", "")
        return AccountSnapshot(cash, pos)

    async def get_daily_bars(self, symbol: str, n: int) -> list[Bar]:
        end = datetime.now(KST_TZ).date()
        q = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
             "FID_INPUT_DATE_1": (end - timedelta(days=int(n * 1.7) + 10)).strftime("%Y%m%d"),
             "FID_INPUT_DATE_2": end.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
        d, _ = await self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                "FHKST03010100", q)
        out = []
        for r in d.get("output2") or []:
            if not r.get("stck_bsop_date"):
                continue
            out.append(Bar(symbol, self._day_epoch(r["stck_bsop_date"]), float(r["stck_oprc"]),
                           float(r["stck_hgpr"]), float(r["stck_lwpr"]), float(r["stck_clpr"]),
                           int(float(r.get("acml_vol") or 0))))
        out.sort(key=lambda b: b.ts)
        return out[-n:]

    async def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if self.env != "real":                           # 휴장일 조회는 실전 도메인에서만 제공
            return True
        try:
            r, _ = await self._call("GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                                    {"BASS_DT": d.strftime("%Y%m%d"), "CTX_AREA_FK": "", "CTX_AREA_NK": ""})
            for row in r.get("output") or []:
                if row.get("bass_dt") == d.strftime("%Y%m%d"):
                    return row.get("opnd_yn") == "Y"
        except Exception as e:
            log.warning("휴장일 조회 실패(평일로 간주): %r", e)
        return True

    async def get_market(self) -> dict:
        out = {}
        for name, code in (("kospi", "0001"), ("kosdaq", "1001")):
            try:
                d, _ = await self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                                        "FHPUP02100000", {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code})
                o = d.get("output") or {}
                out[name] = [float(o.get("bstp_nmix_prpr") or 0), float(o.get("bstp_nmix_prdy_ctrt") or 0)]
            except Exception:
                pass
        return out

    async def get_headlines(self, n: int = 20) -> list[str]:
        if self.env != "real":
            return []
        try:
            d, _ = await self._call("GET", "/uapi/domestic-stock/v1/quotations/news-title", "FHKST01011800", {
                "FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": "",
                "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": "",
                "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": ""})
            return [str(r.get("hts_pbnt_titl_cntt", "")).strip() for r in (d.get("output") or [])[:n]]
        except Exception:
            return []

    # ------------------------------------------------------------------ 실시간(WebSocket)
    async def subscribe(self, symbols: list[str]) -> None:
        symbols = symbols[:MAX_SUBS - 1]                 # 체결통보 1건 + 종목 최대 39개
        old, self._symbols = set(self._symbols), list(symbols)
        ws = self._ws
        if ws is not None and not ws.closed:
            for s in old - set(symbols):
                await self._send_sub(ws, "H0STCNT0", s, "2")
            for s in symbols:
                if s not in old:
                    await self._send_sub(ws, "H0STCNT0", s, "1")

    async def _send_sub(self, ws, tr_id: str, key: str, tr_type: str) -> None:
        await ws.send_str(json.dumps({
            "header": {"approval_key": self._approval, "custtype": "P", "tr_type": tr_type, "content-type": "utf-8"},
            "body": {"input": {"tr_id": tr_id, "tr_key": key}}}))
        await asyncio.sleep(0.05)

    async def _approval_key(self) -> str:
        k = self.cfg.kis
        async with self.session.post(self.base + "/oauth2/Approval", json={
                "grant_type": "client_credentials", "appkey": k.app_key, "secretkey": k.app_secret}) as r:
            d = _loads(await r.read())
        if "approval_key" not in d:
            raise BrokerError(f"웹소켓 접속키 발급 실패: {d}")
        return d["approval_key"]

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                if not self._approval or time.time() - self._approval_ts > 6 * 3600:
                    self._approval = await self._approval_key()
                    self._approval_ts = time.time()
                async with self.session.ws_connect(self.ws_base + "/tryitout", heartbeat=None,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    self._ws = ws
                    await self._subscribe_all(ws)
                    self.ws_connected = True
                    self.on_status("실시간 시세 연결됨")
                    backoff = 1.0
                    self._last_msg = time.monotonic()
                    wd = asyncio.create_task(self._watchdog(ws))
                    try:
                        await self._recv(ws)
                    finally:
                        wd.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("웹소켓 오류: %r", e)
                self._approval = ""                      # 접속키 문제일 수 있으니 다음에는 새로 받는다
            self.ws_connected = False
            if self._stop:
                return
            self.on_status(f"실시간 시세 끊김 — {backoff:.0f}초 후 재연결")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _subscribe_all(self, ws) -> None:
        await self._send_sub(ws, self.tr["notice"], self.cfg.kis.hts_id, "1")
        for s in self._symbols:
            await self._send_sub(ws, "H0STCNT0", s, "1")

    async def _watchdog(self, ws) -> None:
        """끊겼는데 끊긴 줄 모르는 연결(half-open) 감시: 장중에 일정 시간 아무 메시지도 없으면 닫고 다시 붙는다.
        손절·익절이 전부 틱에 달려 있으므로 조용히 멈춘 시세는 가장 위험한 고장이다."""
        while not ws.closed:
            await asyncio.sleep(5.0)
            idle = time.monotonic() - self._last_msg
            if self.expect_data and idle > self.idle_timeout:
                self.on_status(f"실시간 시세 {idle:.0f}초 무응답 — 연결을 끊고 다시 접속합니다")
                await ws.close()
                return

    async def _recv(self, ws) -> None:
        notice = self.tr["notice"]
        on_data = self._on_data
        async for msg in ws:
            self._last_msg = time.monotonic()
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return
                continue
            raw = msg.data
            c = raw[0]
            if c == "0" or c == "1":                     # 실시간 데이터: 암호화여부|TR|건수|본문
                p = raw.split("|", 3)
                if len(p) >= 4:
                    on_data(p[1], c == "1", p[2], p[3])
            else:                                        # 제어 메시지(JSON)
                try:
                    d = _loads(raw)
                except ValueError:
                    continue
                tr_id = (d.get("header") or {}).get("tr_id", "")
                if tr_id == "PINGPONG":
                    await ws.pong(raw.encode())
                    continue
                body = d.get("body") or {}
                out = body.get("output") or {}
                if out.get("key"):                       # 암호화 피드의 키는 TR별로 따로 온다
                    self._aes_by_tr[tr_id] = (out["key"], out["iv"])
                    if tr_id == notice:
                        self._aes = (out["key"], out["iv"])
                msg1 = str(body.get("msg1", ""))
                if body.get("rt_cd") not in (None, "0") and "ALREADY IN SUBSCRIBE" not in msg1:
                    self.on_status(f"구독 응답 오류 {tr_id}: {msg1}")

    def _on_data(self, tr_id: str, encrypted: bool, count: str, payload: str) -> None:
        """실시간 데이터 한 프레임 처리(국내: 체결가 H0STCNT0, 체결통보 H0STCNI0/9)."""
        if tr_id == "H0STCNT0":
            on_tick = self.on_tick
            f = payload.split("^")
            n = int(count) if count.isdigit() else 1
            per = len(f) // n if n > 0 else len(f)
            for i in range(n):
                o = i * per
                try:
                    if per > 43 and f[o + 43] != "0":          # 예상체결(동시호가 등)은 실제 체결이 아님
                        continue
                    t = f[o + 1]
                    ts = (self._day_epoch(f[o + 33]) + int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6]))
                    on_tick(Tick(f[o], ts, float(f[o + 2]), int(f[o + 12]), float(f[o + 11]), float(f[o + 10])))
                except (ValueError, IndexError):
                    continue
        elif tr_id == self.tr["notice"]:
            self._on_notice(payload, encrypted)

    def _on_notice(self, payload: str, encrypted: bool) -> None:
        """체결통보: CNTG_YN=2 체결, RFUS_YN=1 거부."""
        try:
            if encrypted:
                if self._aes is None:
                    return
                payload = aes_cbc_decrypt(self._aes[0], self._aes[1], payload)
            f = payload.split("^")
            odno = f[2].lstrip("0") or "0"
            if f[12] == "1":
                self.on_reject(odno, "주문 거부")
            elif f[13] == "2":
                t = f[11]
                today = datetime.now(KST_TZ).strftime("%Y%m%d")
                ts = self._day_epoch(today) + int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])
                self.on_fill(odno, int(f[9]), float(f[10]), ts)
        except Exception as e:                           # 통보 해석 실패는 폴링이 보완한다
            log.warning("체결통보 해석 실패: %r", e)

    def _day_epoch(self, yyyymmdd: str) -> float:
        v = self._day_cache.get(yyyymmdd)
        if v is None:
            v = datetime(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]), tzinfo=KST_TZ).timestamp()
            self._day_cache[yyyymmdd] = v
        return v
