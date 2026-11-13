"""증권사 연동 인터페이스.

새 증권사(예: 삼성증권이 개인용 API를 열 경우)를 붙이려면 이 클래스를 상속해
아래 메서드만 구현하면 된다. 엔진·에이전트·대시보드는 손댈 필요가 없다.
"""
from __future__ import annotations

from datetime import date
from typing import Awaitable, Callable

from ..models import AccountSnapshot, Bar, Order, Tick


class BrokerError(Exception):
    """주문 거부 등 증권사 측 오류."""


async def _noop(*_a) -> None:
    return None


class Broker:
    name = "base"
    drives_clock = False      # True면 브로커(시뮬레이터)가 장 시작/종료 훅을 직접 호출
    has_account = True        # False면 계좌 대사(reconcile) 생략
    supports_poll = False     # True면 poll_order 로 체결 여부를 다시 물어볼 수 있다
    expect_data = False       # 총괄이 설정: 지금은 시세가 계속 들어와야 하는 시간(무응답이면 재연결)

    def __init__(self) -> None:
        # 속도를 위해 큐 없이 콜백을 직접 호출한다(수신 루프 → 엔진, 같은 스레드)
        self.on_tick: Callable[[Tick], None] = lambda t: None
        self.on_fill: Callable[[str, int, float, float], None] = lambda bid, qty, px, ts: None
        self.on_reject: Callable[[str, str], None] = lambda bid, msg: None
        self.on_poke: Callable[[str], None] = lambda bid: None   # "이 주문에 변화가 있다 — 지금 조회해 보라"
        self.on_cancel: Callable[[str], None] = lambda bid: None  # 증권사(또는 사람) 쪽에서 주문이 취소됨
        self.on_day_start: Callable[[date], Awaitable[None]] = _noop
        self.on_day_end: Callable[[date], Awaitable[None]] = _noop
        self.on_status: Callable[[str], None] = lambda msg: None

    async def start(self) -> None: ...
    async def run(self) -> None: ...          # 시세 수신 루프(종료 요청까지 반환하지 않음)
    async def stop(self) -> None: ...
    async def subscribe(self, symbols: list[str]) -> None: ...

    async def place_order(self, order: Order) -> None:
        """주문 전송. 성공 시 order.broker_id 를 채운다. 거부 시 BrokerError."""
        raise NotImplementedError

    async def cancel_order(self, order: Order) -> None:
        raise NotImplementedError

    async def poll_order(self, order: Order) -> tuple[int, float, bool]:
        """(누적 체결수량, 평균 체결가, 주문 종료 여부). supports_poll 이 True 인 브로커만 구현."""
        raise NotImplementedError

    async def lookup_order(self, order: Order) -> tuple[str, str]:
        """주문 전송 중 오류가 나서 접수 여부를 모를 때 호출.
        ("found", 주문번호) | ("absent", "") = 접수되지 않음이 확실 | ("unknown", "") = 알 수 없음."""
        return "unknown", ""

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    async def get_daily_bars(self, symbol: str, n: int) -> list[Bar]:
        return []

    async def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    async def get_market(self) -> dict:
        """지수 등 시장 요약(시장분석 에이전트용). 없으면 빈 dict."""
        return {}

    async def get_headlines(self, n: int = 20) -> list[str]:
        return []

    async def get_news(self, n: int = 30) -> list[dict]:
        """증권사가 주는 뉴스 제목. [{"ts": epoch, "title": str, "symbols": [..], "source": str}, ...]"""
        return []

    async def get_intraday_bars(self, symbol: str, sec: int) -> list[Bar]:
        """가장 최근 거래일의 분봉(실시간 차트의 지나간 구간 채우기용). 지원하지 않으면 빈 목록."""
        return []

    def now(self) -> float:
        import time
        return time.time()
