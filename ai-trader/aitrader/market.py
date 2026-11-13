"""시장 프로필 — 시장마다 다른 것(시간대·통화·호가단위·주문 방식)을 한곳에 모은다.

엔진은 틱마다 시간대 계산을 하지 않는다. 현지-UTC 시차(초)를 하루에 몇 번만 갱신해 더하기만 한다.
(미국은 서머타임 때문에 시차가 1년에 두 번 바뀐다. 전환은 장이 닫힌 일요일 새벽에 일어난다.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import floor
from zoneinfo import ZoneInfo

from .indicators import tick_size as _krx_tick

DAY = 86400.0


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


@dataclass(frozen=True)
class Market:
    code: str               # KRX | US
    tz: str                 # IANA 시간대
    currency: str           # KRW | USD
    decimals: int           # 가격 표시 소수 자리
    market_orders: bool     # 증권사 API가 '시장가' 주문을 받는가
    emul_pct: float         # 시장가가 없을 때: 호가보다 이만큼 불리한 지정가로 대체
    unit: str               # 금액 표시 단위

    # ------------------------------------------------------------ 호가단위
    def tick(self, price: float, etf: bool = False) -> float:
        if self.code == "KRX":
            return float(_krx_tick(price, etf))
        return 0.01 if price >= 1.0 else 0.0001          # 미국: 1달러 이상 1센트(Reg NMS 612)

    def round_tick(self, price: float, up: bool, etf: bool = False) -> float:
        t = self.tick(price, etf)
        q = price / t
        n = floor(q + 1e-9)
        if up and n < q - 1e-9:
            n += 1
        return round(max(n, 1) * t, 4)

    # ------------------------------------------------------------ 시간
    def utc_offset(self, ts: float) -> float:
        """그 시각의 (현지 − UTC) 초. 서머타임 반영."""
        return datetime.fromtimestamp(ts, ZoneInfo(self.tz)).utcoffset().total_seconds()

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, ZoneInfo(self.tz))

    def local_day(self, ts: float) -> int:
        return int((ts + self.utc_offset(ts)) // DAY)

    def epoch(self, d: date, hh: int = 0, mm: int = 0) -> float:
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(self.tz)).timestamp()

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tz))

    # ------------------------------------------------------------ 표시
    def px(self, price: float) -> str:
        return f"{price:,.{self.decimals}f}"

    def money(self, amount: float, signed: bool = True) -> str:
        s = f"{amount:+,.{self.decimals}f}" if signed else f"{amount:,.{self.decimals}f}"
        return f"{s}원" if self.currency == "KRW" else (s[0] + "$" + s[1:] if signed else "$" + s)


KRX = Market("KRX", "Asia/Seoul", "KRW", 0, True, 0.0, "원")
US = Market("US", "America/New_York", "USD", 2, False, 0.01, "$")
MARKETS = {"KRX": KRX, "US": US}


def get_market(code: str) -> Market:
    try:
        return MARKETS[code.upper()]
    except KeyError:
        raise ValueError(f"알 수 없는 시장: {code} (KRX 또는 US)") from None


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
