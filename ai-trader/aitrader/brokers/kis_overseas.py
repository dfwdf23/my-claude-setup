"""한국투자증권 해외주식(미국) 연동.

스펙 출처: github.com/koreainvestment/open-trading-api 의 overseas_stock 예제.
이 모듈은 실제 KIS 서버가 아니라 스펙을 흉내 낸 가짜 서버(tests/)로만 검증했다. 반드시 모의투자부터.

국내 연동과 다른 점(모두 공식 예제에 근거):
  · 미국 주식은 '시장가' 주문이 없다(지정가·LOO·LOC, 매도는 MOO·MOC 추가). 손절처럼 반드시 체결돼야 하는
    주문은 호가보다 불리한 지정가(기본 1%)로 낸다 — 실제로는 최우선 호가에 체결된다.
  · 거래소 코드가 API마다 다르다: 주문은 NASD/NYSE/AMEX, 시세는 NAS/NYS/AMS, 실시간 키는 D+NAS+종목.
  · 모의투자 서버는 해외 실시간 시세(웹소켓)를 주지 않는다 → 모의에서는 현재가를 주기적으로 조회한다.
    일봉 기준으로 며칠~몇 주 보유하는 방식에는 이 정도 간격이면 충분하다.
  · 체결통보의 수량·가격 표기는 문서가 불완전하다(가격은 정수로 오고 소수점 위치가 나라마다 다름,
    부분 체결이 누적인지 건별인지 불명). 그래서 통보는 '지금 조회해 보라'는 신호로만 쓰고,
    체결 수량·가격은 주문체결내역 조회 결과를 기준으로 삼는다.
확인하지 못한 것: 모의투자 매도 TR(문서 VTTT1001U vs 예제 코드 VTTT1006U — 첫 매도 때 둘 다 시도),
모의투자에서 매수가능금액 조회 지원 여부, 취소된 주문의 미체결수량 표기.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..market import is_trading_day
from ..models import BUY, AccountSnapshot, Bar, Order, Tick
from .base import BrokerError, BrokerUnknown
from .kis import KISBroker, aes_cbc_decrypt

log = logging.getLogger("aitrader.kis")
KST_TZ = timezone(timedelta(hours=9))
TR_OS = {
    "real": {"buy": "TTTT1002U", "sell": "TTTT1006U", "cancel": "TTTT1004U", "balance": "TTTS3012R",
             "ccnl": "TTTS3035R", "psamount": "TTTS3007R", "present": "CTRP6504R", "notice": "H0GSCNI0"},
    "mock": {"buy": "VTTT1002U", "sell": "VTTT1001U", "cancel": "VTTT1004U", "balance": "VTTS3012R",
             "ccnl": "VTTS3035R", "psamount": "VTTS3007R", "present": "VTRP6504R", "notice": "H0GSCNI9"},
}
ORDER_EXCH = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
MOCK_SELL_ALT = "VTTT1006U"
MAX_PAGES = 40                # 주문체결내역 조회 한 번에 읽는 최대 쪽수


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm(odno) -> str:
    return str(odno or "").lstrip("0") or "0"


class KISOverseasBroker(KISBroker):
    name = "kis-us"
    need_hts = False           # 체결은 조회로 확인하므로 HTS ID 가 없어도 동작한다(있으면 통보를 받아 더 빨리 확인)
    TR_TABLE = TR_OS

    def __init__(self, cfg: Config, base_url: str = "", ws_url: str = "") -> None:
        super().__init__(cfg, base_url, ws_url)
        self.m = cfg.mkt
        self.exch: dict[str, str] = {k: v.upper() for k, v in cfg.kis.exchanges.items()}   # 종목 → NAS/NYS/AMS
        self.last: dict[str, Tick] = {}
        self.stream = self.env == "real"          # 실전: 웹소켓 실시간. 모의: 현재가 조회(폴링)
        self.poll_gap = 1.2                       # 모의투자 호출 한도(초당 약 2건) 안에서 주문용 여유를 남긴다
        self.emul_pct = cfg.mkt.emul_pct
        self._cum: dict[str, int] = {}
        self._known: set[str] = set()             # 이 프로세스가 낸 주문번호
        self._sell_tr = ""                        # 모의 매도 TR 중 실제로 통한 것
        self._news_first = True
        self._titles: list[str] = []
        self._oh, self._om = divmod(cfg.session.tod("open") // 60, 60)
        self._exch_file = Path(cfg.data_dir) / cfg.mode / "kis_exchanges.json"
        try:
            self.exch = {**json.loads(self._exch_file.read_text()), **self.exch}
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ 종목 → 거래소
    async def _price(self, symbol: str, excd: str | None = None) -> dict:
        d, _ = await self._call("GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                                {"AUTH": "", "EXCD": excd or await self._exchange(symbol), "SYMB": symbol})
        return d.get("output") or {}

    async def _exchange(self, symbol: str) -> str:
        """시세용 3글자 거래소 코드. 설정에 없으면 세 거래소에 차례로 물어 현재가가 나오는 곳을 쓴다."""
        e = self.exch.get(symbol)
        if e:
            return e
        for excd in ("NAS", "NYS", "AMS"):
            try:
                o = await self._price(symbol, excd)
            except BrokerError:
                continue
            if _f(o.get("last")) > 0:
                self.exch[symbol] = excd
                try:
                    self._exch_file.parent.mkdir(parents=True, exist_ok=True)
                    self._exch_file.write_text(json.dumps(self.exch))
                except OSError:
                    pass
                return excd
        raise BrokerError(f"{symbol} 의 거래소를 찾지 못했습니다 — config.toml 의 [kis.exchanges] 에 지정하세요")

    # ------------------------------------------------------------------ 주문
    def _px(self, price: float) -> str:
        return f"{price:.2f}" if price >= 1.0 else f"{price:.4f}"

    async def place_order(self, order: Order) -> None:
        buy = order.side == BUY
        price = order.limit_price
        if not price:
            # 시장가 대체: 반대편 호가보다 emul_pct 만큼 불리한 지정가(체결은 최우선 호가에 된다)
            t = self.last.get(order.symbol)
            ref = ((t.ask or t.price) if buy else (t.bid or t.price)) if t else 0.0
            if ref <= 0:
                raise BrokerError("현재가를 몰라 시장가 대체 주문을 낼 수 없습니다")
            price = (self.m.round_tick(ref * (1 + self.emul_pct), True) if buy
                     else self.m.round_tick(ref * (1 - self.emul_pct), False))
        body = {**self._acct(), "OVRS_EXCG_CD": ORDER_EXCH[await self._exchange(order.symbol)],
                "PDNO": order.symbol, "ORD_QTY": str(order.qty), "OVRS_ORD_UNPR": self._px(price),
                "CTAC_TLNO": "", "MGCO_APTM_ODNO": "", "SLL_TYPE": "" if buy else "00",
                "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"}
        path = "/uapi/overseas-stock/v1/trading/order"
        tr = self.tr["buy"] if buy else (self._sell_tr or self.tr["sell"])
        try:
            d, _ = await self._call("POST", path, tr, body)
        except BrokerError as first:
            if buy or self.env != "mock" or self._sell_tr:
                raise
            # 모의 매도 TR은 문서(VTTT1001U)와 예제 코드(VTTT1006U)가 다르고, 어느 쪽이 맞는지는 매도가 한 번
            # 성공해야 알 수 있다. 거부된 주문(=접수 안 됨)만 다른 TR로 한 번 더 보내 보고, 그것도 거부되면
            # 처음 거부 사유를 그대로 올린다 — 잔고 부족 같은 평범한 거부를 'TR이 틀렸다'로 단정해
            # 한쪽에 고정해 버리면 이후 매도가 전부 막힌다.
            try:
                d, _ = await self._call("POST", path, MOCK_SELL_ALT, body)
            except BrokerError:
                raise first from None
            self._sell_tr = MOCK_SELL_ALT
        else:
            if not buy and not self._sell_tr:
                self._sell_tr = tr                        # 한 번 성공한 TR로 고정
        out = d.get("output") or {}
        odno = str(out.get("ODNO", ""))
        if not odno:
            raise BrokerUnknown("주문은 성공 응답인데 주문번호가 없습니다")      # 접수됐을 가능성이 높다 → 조회로 확정
        key = _norm(odno)
        self._raw[key] = (str(out.get("KRX_FWDG_ORD_ORGNO", "")), odno)
        self._known.add(key)
        order.broker_id = key
        order.broker_org = self._raw[key][0]

    async def cancel_order(self, order: Order) -> None:
        _, odno = self._raw.get(order.broker_id, ("", order.broker_id.zfill(10)))
        body = {**self._acct(), "OVRS_EXCG_CD": ORDER_EXCH[await self._exchange(order.symbol)],
                "PDNO": order.symbol, "ORGN_ODNO": odno, "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(max(order.qty - order.filled, 0)), "OVRS_ORD_UNPR": "0",
                "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"}
        await self._call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", self.tr["cancel"], body)

    async def _orders_today(self, symbol: str, want: str = "") -> list[dict]:
        """주문체결내역(현지 날짜 기준 어제~오늘). 주문번호로는 검색할 수 없어 받아서 고른다.

        want: 찾는 주문번호. 실전은 종목별·최신순이라 찾으면 거기서 멈춘다. 모의는 전 종목·오래된 순으로만
        오므로(정렬 변경 불가) 끝까지 읽어야 최신 주문이 보인다."""
        today = self.m.now().date()
        real = self.env == "real"
        q = {**self._acct(), "PDNO": symbol if real else "", "ORD_STRT_DT": (today - timedelta(days=1)).strftime("%Y%m%d"),
             "ORD_END_DT": today.strftime("%Y%m%d"), "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
             "OVRS_EXCG_CD": "NASD" if real else "", "SORT_SQN": "AS" if real else "DS", "ORD_DT": "",
             "ORD_GNO_BRNO": "", "ODNO": "", "CTX_AREA_NK200": "", "CTX_AREA_FK200": ""}
        rows: list[dict] = []
        cont = ""
        for page in range(MAX_PAGES):
            d, nxt = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", self.tr["ccnl"], q, cont)
            got = d.get("output") or []
            rows += got
            if nxt not in ("F", "M"):
                break
            if want and real and any(_norm(r.get("odno")) == want for r in got):
                break
            if page == MAX_PAGES - 1:
                # 더 있는데 못 읽었다 — '주문 없음'으로 오해하면 접수된 주문을 놓친다. 모르는 것은 모른다고 올린다.
                raise BrokerUnknown(f"주문체결내역이 {MAX_PAGES}쪽을 넘어 끝까지 읽지 못했습니다")
            cont = "N"
            q["CTX_AREA_FK200"] = d.get("ctx_area_fk200", "")
            q["CTX_AREA_NK200"] = d.get("ctx_area_nk200", "")
        return rows

    async def poll_order(self, order: Order) -> tuple[int, float, bool]:
        for row in await self._orders_today(order.symbol, order.broker_id):
            if _norm(row.get("odno")) != order.broker_id:
                continue
            filled = _i(row.get("ft_ccld_qty"))
            price = _f(row.get("ft_ccld_unpr3"))
            if price <= 0 and filled > 0:
                price = _f(row.get("ft_ccld_amt3")) / filled
            status = str(row.get("prcs_stat_name", ""))
            rejected = bool(str(row.get("rjct_rson", "")).strip()) or "거부" in status
            done = _i(row.get("nccs_qty")) == 0 or rejected or "취소" in status
            return filled, price, done
        return order.filled, 0.0, False

    async def lookup_order(self, order: Order) -> tuple[str, str]:
        try:
            rows = await self._orders_today(order.symbol)
        except Exception:
            return "unknown", ""
        side = "02" if order.side == BUY else "01"
        best = ""
        for row in rows:
            key = _norm(row.get("odno"))
            if (key in self._known or row.get("pdno") != order.symbol or row.get("sll_buy_dvsn_cd") != side
                    or _i(row.get("ft_ord_qty")) != order.qty or str(row.get("rvse_cncl_dvsn", "")).strip() not in ("", "00")):
                continue
            if not best or int(key) > int(best):
                best = key
                self._raw[key] = (str(row.get("ord_gno_brno", "")), str(row.get("odno")))
        if not best:
            return "absent", ""
        self._known.add(best)
        return "found", best

    # ------------------------------------------------------------------ 계좌
    async def _holdings(self, excg: str) -> dict:
        q = {**self._acct(), "OVRS_EXCG_CD": excg, "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        pos: dict = {}
        cont = ""
        for _ in range(6):
            d, nxt = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", self.tr["balance"], q, cont)
            for row in d.get("output1") or []:
                qty = _i(row.get("ovrs_cblc_qty"))
                if qty > 0:
                    pos[row.get("ovrs_pdno")] = (qty, _f(row.get("pchs_avg_pric")))
            if nxt not in ("F", "M"):
                break
            cont = "N"
            q["CTX_AREA_FK200"] = d.get("ctx_area_fk200", "")
            q["CTX_AREA_NK200"] = d.get("ctx_area_nk200", "")
        return pos

    async def _cash(self) -> float:
        """주문에 쓸 수 있는 달러. 매수가능금액 조회 → 안 되면 체결기준현재잔고."""
        sym = self._symbols[0] if self._symbols else (next(iter(self.exch), "") or "AAPL")
        t = self.last.get(sym)
        try:
            d, _ = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-psamount", self.tr["psamount"], {
                **self._acct(), "OVRS_EXCG_CD": ORDER_EXCH[await self._exchange(sym)],
                "OVRS_ORD_UNPR": self._px(t.price) if t else "1", "ITEM_CD": sym})
            o = d.get("output") or {}
            v = _f(o.get("ord_psbl_frcr_amt"))
            if v > 0 or "ord_psbl_frcr_amt" in o:
                return v
        except BrokerError as e:
            log.info("매수가능금액 조회 불가(%s) — 현재잔고로 대체", e)
        d, _ = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-present-balance", self.tr["present"], {
            **self._acct(), "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"})
        for row in d.get("output2") or []:
            if row.get("crcy_cd") == "USD" and _f(row.get("frcr_dncl_amt_2")) > 0:
                return _f(row.get("frcr_dncl_amt_2"))
        o3 = d.get("output3") or {}
        o3 = o3[0] if isinstance(o3, list) and o3 else o3
        return _f(o3.get("frcr_use_psbl_amt")) if isinstance(o3, dict) else 0.0

    async def get_account(self) -> AccountSnapshot:
        pos: dict = {}
        # 실전은 NASD 한 번으로 미국 전체, 모의는 거래소별로 따로 조회해야 한다
        for excg in (("NASD",) if self.env == "real" else ("NASD", "NYSE", "AMEX")):
            pos.update(await self._holdings(excg))
        return AccountSnapshot(await self._cash(), pos)

    # ------------------------------------------------------------------ 조회
    async def get_daily_bars(self, symbol: str, n: int) -> list[Bar]:
        excd = await self._exchange(symbol)
        seen: dict[str, Bar] = {}
        bymd = ""
        for _ in range(6):
            d, _ = await self._call("GET", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000", {
                "AUTH": "", "EXCD": excd, "SYMB": symbol, "GUBN": "0", "BYMD": bymd, "MODP": "1"})
            rows = [r for r in d.get("output2") or [] if r.get("xymd") and _f(r.get("clos")) > 0]
            before = len(seen)
            for r in rows:
                x = r["xymd"]
                if x not in seen:
                    dt = date(int(x[:4]), int(x[4:6]), int(x[6:8]))
                    seen[x] = Bar(symbol, self.m.epoch(dt, self._oh, self._om), _f(r.get("open")), _f(r.get("high")),
                                  _f(r.get("low")), _f(r.get("clos")), _i(r.get("tvol")))
            if len(seen) >= n or len(seen) == before or not rows:
                break
            bymd = min(seen)                              # 가장 오래된 날짜부터 이어서 조회
        return [seen[k] for k in sorted(seen)][-n:]

    async def is_trading_day(self, d: date) -> bool:
        return is_trading_day(self.m, d)

    async def get_market(self) -> dict:
        return {}

    async def get_news(self, n: int = 30) -> list[dict]:
        """해외뉴스종합 제목(실전 전용). 시각의 기준 시간대가 문서에 없어 한국시간으로 보고, 어긋나면 지금 시각으로 둔다."""
        if self.env != "real":
            return []
        try:
            d, _ = await self._call("GET", "/uapi/overseas-price/v1/quotations/news-title", "HHPSTH60100C1", {
                "INFO_GB": "", "CLASS_CD": "", "NATION_CD": "US", "EXCHANGE_CD": "", "SYMB": "",
                "DATA_DT": "", "DATA_TM": "", "CTS": ""})
        except Exception:
            return []
        now = time.time()
        out = []
        for r in (d.get("outblock1") or [])[:n]:
            title = str(r.get("title", "")).strip()
            if not title:
                continue
            ts = now
            try:
                t = datetime.strptime(str(r.get("data_dt", "")) + str(r.get("data_tm", "")).zfill(6), "%Y%m%d%H%M%S")
                cand = t.replace(tzinfo=KST_TZ).timestamp()
                if 0 <= now - cand <= 86400:
                    ts = cand
            except ValueError:
                pass
            sym = str(r.get("symb", "")).strip()
            out.append({"ts": ts, "title": title, "symbols": [sym] if sym else [], "source": str(r.get("source", "")) or "KIS",
                        "stale": self._news_first})     # 시작 직후 받은 묵은 목록은 매매 판단에 쓰지 않는다
        self._news_first = False
        self._titles = [x["title"] for x in out[:15]]
        return out

    async def get_headlines(self, n: int = 20) -> list[str]:
        return self._titles[:n]

    # ------------------------------------------------------------------ 시세
    async def subscribe(self, symbols: list[str]) -> None:
        symbols = symbols[:38]
        for s in symbols:
            await self._exchange(s)
        old, self._symbols = set(self._symbols), list(symbols)
        ws = self._ws
        if self.stream and ws is not None and not ws.closed:
            for s in old - set(symbols):
                await self._send_sub(ws, "HDFSCNT0", "D" + self.exch[s] + s, "2")
            for s in symbols:
                if s not in old:
                    await self._send_sub(ws, "HDFSCNT0", "D" + self.exch[s] + s, "1")

    async def _subscribe_all(self, ws) -> None:
        if self.cfg.kis.hts_id:
            await self._send_sub(ws, self.tr["notice"], self.cfg.kis.hts_id, "1")
        for s in self._symbols:
            await self._send_sub(ws, "HDFSCNT0", "D" + self.exch[s] + s, "1")

    async def run(self) -> None:
        if self.stream:
            await super().run()
            return
        self.on_status("모의투자: 해외 실시간 시세가 제공되지 않아 현재가를 주기적으로 조회합니다")
        while not self._stop:                            # 모의: 장중에만 현재가를 돌아가며 조회
            syms = list(self._symbols)
            if not syms or not self.expect_data:
                await asyncio.sleep(1.0)
                continue
            for sym in syms:
                if self._stop or not self.expect_data:
                    break
                try:
                    o = await self._price(sym)
                except Exception as e:
                    log.debug("현재가 조회 실패 %s: %r", sym, e)
                    await asyncio.sleep(self.poll_gap)
                    continue
                last, tvol = _f(o.get("last")), _i(o.get("tvol"))
                if last > 0:
                    prev = self._cum.get(sym, tvol)
                    self._cum[sym] = tvol
                    t = Tick(sym, time.time(), last, max(tvol - prev, 0), 0.0, 0.0, tvol)
                    self.last[sym] = t
                    self.on_tick(t)
                await asyncio.sleep(self.poll_gap)

    def _on_data(self, tr_id: str, encrypted: bool, count: str, payload: str) -> None:
        if tr_id == "HDFSCNT0":
            f = payload.split("^")
            n = int(count) if count.isdigit() and int(count) > 0 else 1
            per = len(f) // n
            if per not in (25, 26):                      # 문서와 다른 형식이면 추측해서 읽지 않는다
                self._bad_frames = getattr(self, "_bad_frames", 0) + 1
                if self._bad_frames <= 3:
                    self.on_status(f"해외 체결가 형식이 예상과 다릅니다(필드 {per}개) — 시세를 반영하지 않습니다")
                return
            shift = 0 if per == 26 else -1               # 맨 앞 실시간키(RSYM)가 빠진 25필드 변형 대비
            for i in range(n):
                o = i * per + shift
                try:
                    if f[o + 25] != "1":                 # 1 장중 / 2 장전 / 3 장후 — 정규장만 쓴다
                        continue
                    sym, d, hms = f[o + 1], f[o + 4], f[o + 5].zfill(6)
                    ts = self._day_epoch_local(d) + int(hms[:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6])
                    t = Tick(sym, ts, float(f[o + 11]), _i(f[o + 19]), _f(f[o + 15]), _f(f[o + 16]), _i(f[o + 20]))
                except (ValueError, IndexError):
                    continue
                self.last[sym] = t
                self.on_tick(t)
        elif tr_id == self.tr["notice"]:
            try:
                if encrypted:
                    aes = self._aes_by_tr.get(tr_id)
                    if aes is None:
                        return
                    payload = aes_cbc_decrypt(aes[0], aes[1], payload)
                f = payload.split("^")
                odno = _norm(f[2])
                if f[11] == "1":
                    self.on_reject(odno, "주문 거부")
                else:
                    self.on_poke(odno)                    # 접수·체결 통보 → 조회로 수량·가격 확인
            except Exception as e:
                log.warning("해외 체결통보 해석 실패: %r", e)

    def _day_epoch_local(self, yyyymmdd: str) -> float:
        v = self._day_cache.get("L" + yyyymmdd)
        if v is None:
            v = self.m.epoch(date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])))
            self._day_cache["L" + yyyymmdd] = v
        return v
