"""주문 보조 모드(mode = manual) — 봇이 '무엇을 얼마에 사고팔지'까지 계산하고, 주문 버튼은 사람이 누른다.

매매 API가 없는 증권사(예: 삼성증권) 계좌로 쓰기 위한 방식이다.
  1. 봇이 신호를 내면 주문 제안(티켓)이 대시보드와 알림(텔레그램)에 뜬다.
  2. 사용자가 증권사 앱에서 직접 주문하고, 대시보드에서 [체결됨]을 눌러 실제 체결가를 알려 준다.
  3. 봇은 그 체결을 장부에 반영해 손절·익절·악재 청산 시점을 계속 감시하고, 때가 되면 매도 제안을 낸다.
제안은 일정 시간(기본 5분) 안에 응답이 없으면 만료된다. 사람이 자는 시간에는 아무 주문도 나가지 않는다.

시세는 야후 파이낸스의 공개 조회 주소를 주기적으로 읽는다(비공식·지연 가능·약관상 개인적 이용).
자동 주문이 아니라 사람이 확인하고 누르는 방식이므로 이 정도 시세 품질로 충분하다고 본다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Callable

from ..config import Config
from ..market import is_trading_day
from ..models import AccountSnapshot, Bar, Order, Tick
from .base import Broker, BrokerError

log = logging.getLogger("aitrader.manual")
YAHOO = "https://query1.finance.yahoo.com"
UA = "Mozilla/5.0 (compatible; aitrader-manual)"
INTERVALS = {60: "1m", 120: "2m", 300: "5m", 900: "15m", 1800: "30m", 3600: "60m"}


class ManualBroker(Broker):
    name = "manual"
    has_account = False

    def __init__(self, cfg: Config, base_url: str = YAHOO) -> None:
        super().__init__()
        self.cfg = cfg
        self.m = cfg.mkt
        if self.m.code != "US":
            raise SystemExit("주문 보조 모드의 시세 조회는 현재 미국 시장만 지원합니다(--market us)")
        self.base = base_url
        self.poll_sec = cfg.manual.poll_sec
        self.session = None
        self.symbols: list[str] = []
        self.tickets: dict[str, dict] = {}
        self.last: dict[str, float] = {}
        self._cum: dict[str, int] = {}
        self._n = 0
        self._stop = False
        self.errors = 0
        self.stale = 0                        # 너무 오래돼 버린 시세(지연·거래정지) 건수
        self.max_quote_age = 900.0            # 마지막 체결이 이보다 오래된 시세는 판단에 쓰지 않는다(초)
        self.on_ticket: Callable[[dict], None] = lambda t: None      # 새 주문 제안(알림용)
        # 만료·건너뛴 제안을 사람이 뒤늦게 체결했다고 알릴 때(총괄이 장부에 직접 반영): (제안, 수량, 가격) → (성공, 메시지)
        self.on_late: Callable[[dict, int, float], tuple[bool, str]] = lambda t, q, px: (False, "늦은 체결을 반영할 수 없습니다")
        self.late_sec = cfg.manual.snooze_min * 60.0                  # 만료 뒤 이 시간까지는 늦은 체결 확인을 받는다
        self._oh, self._om = divmod(cfg.session.tod("open") // 60, 60)

    async def start(self) -> None:
        import aiohttp
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": UA})

    async def stop(self) -> None:
        self._stop = True
        if self.session is not None:
            await self.session.close()

    async def subscribe(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)

    # ------------------------------------------------------------------ 시세
    async def _quotes(self, symbols: list[str]) -> list[tuple[str, float, float, int]]:
        """[(종목, 현재가, 체결시각, 당일 누적 거래량)]"""
        url = f"{self.base}/v7/finance/spark"
        async with self.session.get(url, params={"symbols": ",".join(symbols), "range": "1d", "interval": "1m"}) as r:
            if r.status != 200:
                raise BrokerError(f"시세 조회 실패 HTTP {r.status}")
            d = await r.json(content_type=None)
            self._note_date(r.headers)
        out = []
        for res in (d.get("spark") or {}).get("result") or []:
            try:
                m = res["response"][0]["meta"]
                px, ts = float(m["regularMarketPrice"]), float(m["regularMarketTime"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if px > 0:
                out.append((str(res.get("symbol") or m.get("symbol")), px, ts, int(m.get("regularMarketVolume") or 0)))
        return out

    async def run(self) -> None:
        while not self._stop:
            if not self.symbols or not self.expect_data:
                await asyncio.sleep(1.0)
                continue
            try:
                quotes = await self._quotes(self.symbols)
                now = time.time()
                for sym, px, mts, cum in quotes:
                    if now - mts > self.max_quote_age:
                        self.stale += 1                 # 지연된 시세를 현재가로 착각하지 않는다 → 시세 끊김 감지에 맡긴다
                        continue
                    prev = self._cum.get(sym, cum)
                    self._cum[sym] = cum
                    self.last[sym] = px
                    # 틱 시각은 조회 시각 — 종목마다 마지막 체결 시각이 달라 엔진 시계가 앞뒤로 흔들리는 것을 막는다
                    self.on_tick(Tick(sym, now, px, max(cum - prev, 0), 0.0, 0.0, cum))
            except Exception as e:
                self.errors += 1
                log.debug("시세 조회 오류: %r", e)
            await asyncio.sleep(self.poll_sec)

    async def _chart(self, symbol: str, interval: str, rng: str) -> list[tuple]:
        """[(시각, 시가, 고가, 저가, 종가, 거래량)] — 값이 빠진 행(휴장·결측)은 제외."""
        async with self.session.get(f"{self.base}/v8/finance/chart/{symbol}",
                                    params={"interval": interval, "range": rng}) as r:
            if r.status != 200:
                return []
            d = await r.json(content_type=None)
        try:
            res = d["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"], q["volume"])
            return [(float(ts), float(o), float(h), float(l), float(c), int(v or 0))
                    for ts, o, h, l, c, v in rows if None not in (o, h, l, c) and c > 0]
        except (KeyError, IndexError, TypeError, ValueError):
            return []

    async def get_daily_bars(self, symbol: str, n: int) -> list[Bar]:
        out = []
        for ts, o, h, l, c, v in await self._chart(symbol, "1d", "1y" if n <= 250 else "2y"):
            day = self.m.local(ts).date()                           # 시각은 그날 장 시작으로 맞춘다
            out.append(Bar(symbol, self.m.epoch(day, self._oh, self._om), o, h, l, c, v))
        return out[-n:]

    async def get_intraday_bars(self, symbol: str, sec: int) -> list[Bar]:
        """가장 최근 거래일의 분봉 — 장중에 켰을 때 실시간 차트의 지나간 구간을 채우는 용도(화면 전용)."""
        iv = INTERVALS.get(int(sec))
        if iv is None:
            return []
        return [Bar(symbol, ts, o, h, l, c, v) for ts, o, h, l, c, v in await self._chart(symbol, iv, "1d")]

    async def is_trading_day(self, d: date) -> bool:
        return is_trading_day(self.m, d)

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(self.cfg.capital, {})

    # ------------------------------------------------------------------ 주문 제안(티켓)
    async def place_order(self, order: Order) -> None:
        self._n += 1
        tid = f"M{self._n}"
        order.broker_id = tid
        t = {"id": tid, "symbol": order.symbol, "side": order.side, "qty": order.qty,
             "price": order.limit_price, "market": not order.limit_price, "reason": order.reason,
             "ref": self.last.get(order.symbol, 0.0), "created": time.time(), "status": "OPEN",
             "stop": 0.0, "fill_qty": 0, "fill_price": 0.0, "closed": 0.0, "ctx": dict(order.ctx or {})}
        sd = (order.ctx or {}).get("stop_dist")
        if sd and order.limit_price:
            t["stop"] = round(order.limit_price - sd, 4)              # 매수 제안에는 손절 예정가를 함께 보여 준다
        self.tickets[tid] = t
        while len(self.tickets) > 200:
            self.tickets.pop(next(iter(self.tickets)))
        self.on_ticket(t)

    async def cancel_order(self, order: Order) -> None:
        t = self.tickets.get(order.broker_id)
        if t is None or t["status"] != "OPEN":
            raise BrokerError("이미 처리된 제안")
        t["status"], t["closed"] = "EXPIRED", time.time()

    def late_ok(self, t: dict, now: float | None = None) -> bool:
        """만료·건너뛴 제안이라도 얼마 동안은 '사실은 체결했다'는 확인을 받는다(주문하느라 5분을 넘기는 일은 흔하다)."""
        return t["status"] in ("EXPIRED", "SKIPPED") and (now or time.time()) - t["closed"] <= self.late_sec

    def confirm(self, tid: str, qty: int | None = None, price: float | None = None) -> tuple[bool, str]:
        """사용자가 '직접 주문해서 체결됐다'고 알림. 수량·가격을 안 주면 제안값을 쓴다. (반영 여부, 메시지)"""
        t = self.tickets.get(tid)
        if t is None:
            return False, "없는 제안입니다"
        late = self.late_ok(t)
        if t["status"] != "OPEN" and not late:
            return False, "이미 처리됐거나 만료된 지 오래된 제안입니다 — 장부에 반영되지 않았습니다"
        q = t["qty"] if qty is None else int(qty)
        px = float(price) if price else (t["price"] or self.last.get(t["symbol"], 0.0) or t["ref"])
        if q <= 0 or q > t["qty"] or not px > 0:
            return False, "수량·가격을 확인하세요 — 장부에 반영되지 않았습니다"
        if not self.plausible(t["symbol"], px, t["price"] or t["ref"]):
            return False, "입력한 가격이 현재가와 너무 다릅니다 — 가격을 확인하세요(장부에 반영되지 않았습니다)"
        if late:                                                      # 주문은 이미 끝난 것으로 정리됐다 → 장부에 직접 반영
            ok, msg = self.on_late(t, q, px)
            if ok:
                t["status"], t["fill_qty"], t["fill_price"] = "FILLED", q, px
            return ok, msg
        t["status"], t["fill_qty"], t["fill_price"], t["closed"] = "FILLED", q, px, time.time()
        self.on_fill(tid, q, px, time.time())
        if q < t["qty"]:
            self.on_cancel(tid)                                       # 남은 수량은 주문하지 않은 것으로 본다
        return True, f"{t['symbol']} {t['side']} {q}주 @ {self.m.px(px)} 체결 반영"

    def plausible(self, symbol: str, px: float, ref: float = 0.0) -> bool:
        """잘못 입력한 가격(자릿수 실수 등)이 장부에 들어가지 않게: 현재가(없으면 기준가)와 20% 넘게 다르면 거부."""
        ref = self.last.get(symbol, 0.0) or ref
        return not ref > 0 or abs(px / ref - 1.0) <= 0.2

    def skip(self, tid: str) -> tuple[bool, str]:
        t = self.tickets.get(tid)
        if t is None or t["status"] != "OPEN":
            return False, "이미 처리됐거나 만료된 제안입니다"
        t["status"], t["closed"] = "SKIPPED", time.time()
        self.on_cancel(tid)
        return True, f"{t['symbol']} 제안을 건너뜀"

    def tickets_view(self) -> list[dict]:
        now = time.time()
        out = []
        for t in list(self.tickets.values())[-20:][::-1]:
            v = {k: x for k, x in t.items() if k != "ctx"}            # 내부용 정보는 화면으로 보내지 않는다
            v.update(age=round(now - t["created"]), ttl=self.cfg.manual.ticket_ttl_min * 60.0,
                     late_ok=self.late_ok(t, now), late_left=max(0, round(self.late_sec - (now - t["closed"]))))
            out.append(v)
        return out
