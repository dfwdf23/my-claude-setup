"""주문 집행 에이전트 — 엔진의 주문 의도를 증권사로 보내고 체결을 엔진에 돌려준다.

submit() 은 동기 함수다: 엔진(핫패스)은 네트워크 왕복을 기다리지 않고 바로 다음 틱을 처리한다.
"""
from __future__ import annotations

import asyncio
import time
from time import perf_counter_ns

from .agents.base import Agent
from .brokers.base import Broker, BrokerError
from .engine import Engine, LatencyStats
from .models import Fill, Order, OrderIntent


class Executor(Agent):
    name = "executor"
    role = "주문 집행"

    def __init__(self, broker: Broker, engine: Engine, timeout_sec: float = 10.0) -> None:
        super().__init__()
        self.broker = broker
        self.engine = engine
        self.timeout = timeout_sec
        self.orders: dict[int, Order] = {}            # 진행 중 주문
        self.by_bid: dict[str, Order] = {}
        self.early: dict[str, list] = {}              # 주문 응답보다 먼저 도착한 체결통보
        self.ws_cum: dict[int, int] = {}              # 주문별 '통보로 받은' 누적 체결수량
        self.polled: dict[int, float] = {}            # 주문별 마지막 폴링 시각
        self.can_poll = bool(getattr(broker, "supports_poll", False))
        self.recent: dict[str, Order] = {}            # 방금 끝난 주문(취소 직후 도착하는 늦은 체결 대비)
        self.rtt = LatencyStats(1024)                 # 주문 전송→접수 응답(ms×1000)
        self._n = 0
        self._tasks: set = set()
        broker.on_fill = self.on_fill
        broker.on_reject = self.on_reject
        engine.execute = self.submit

    def submit(self, it: OrderIntent) -> None:
        self.calls += 1
        self._n += 1
        o = Order(self._n, it.symbol, it.side, it.qty, it.limit_price, it.reason, it.ctx,
                  self.engine.now, status="SENT", sent_ns=perf_counter_ns())
        self.orders[o.id] = o
        t = asyncio.get_running_loop().create_task(self._place(o))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _place(self, o: Order) -> None:
        try:
            await self.broker.place_order(o)
        except BrokerError as e:
            self._finish(o, "REJECTED", str(e), rejected=True)
            return
        except Exception as e:                       # 네트워크 오류 등 — 체결 여부 불명
            self._finish(o, "REJECTED", f"전송 오류: {e!r}", rejected=True)
            return
        o.rtt_ms = (perf_counter_ns() - o.sent_ns) / 1e6
        self.rtt.add(int(o.rtt_ms * 1000))
        if o.status == "SENT":
            o.status = "ACK"
        self.by_bid[o.broker_id] = o
        for args in self.early.pop(o.broker_id, ()):
            self.on_fill(*args)

    def on_fill(self, broker_id: str, qty: int, price: float, ts: float) -> None:
        """실시간 체결통보(증분). 폴링(누적)과 겹쳐도 이중 반영되지 않게 누적 기준으로 합친다."""
        o = self.by_bid.get(broker_id)
        if o is None:
            late = self.recent.get(broker_id)
            if late is not None:                      # 취소 처리 후 도착한 체결 — 장부에는 반영해야 한다
                q = min(qty, late.qty - late.filled)
                if q > 0:
                    late.filled += q
                    self.engine.on_fill(Fill(late.symbol, late.side, q, price, ts, late.id, late.reason, late.ctx))
                return
            self.early.setdefault(broker_id, []).append((broker_id, qty, price, ts))
            if len(self.early) > 200:                 # 봇이 낸 주문이 아닌 통보(수동 주문 등)는 버린다
                self.early.pop(next(iter(self.early)))
            return
        cum = self.ws_cum.get(o.id, 0) + qty
        self.ws_cum[o.id] = cum
        self._apply(o, cum, price, ts)

    def _apply(self, o: Order, cum: int, price: float, ts: float) -> None:
        qty = min(cum, o.qty) - o.filled
        if qty <= 0:
            return
        o.avg_price = (o.avg_price * o.filled + price * qty) / (o.filled + qty)
        o.filled += qty
        self.note(f"{o.symbol} {o.side} {qty}주 @ {price:,.0f} ({o.rtt_ms:.1f}ms)", ts)
        self.engine.on_fill(Fill(o.symbol, o.side, qty, price, ts, o.id, o.reason, o.ctx))
        if o.filled >= o.qty:
            self._finish(o, "FILLED")

    def on_reject(self, broker_id: str, msg: str) -> None:
        o = self.by_bid.get(broker_id)
        if o is not None and o.status in ("SENT", "ACK"):
            self._finish(o, "REJECTED", msg, rejected=True)

    def _finish(self, o: Order, status: str, reason: str = "", rejected: bool = False) -> None:
        o.status = status
        o.error = reason
        self.orders.pop(o.id, None)
        self.by_bid.pop(o.broker_id, None)
        self.ws_cum.pop(o.id, None)
        self.polled.pop(o.id, None)
        if o.broker_id and o.filled < o.qty:
            self.recent[o.broker_id] = o
            if len(self.recent) > 100:
                self.recent.pop(next(iter(self.recent)))
        # 일부라도 체결됐으면 성공으로 본다(남은 수량은 다음 판단에 맡김)
        self.engine.on_order_done(o.symbol, status == "FILLED" or o.filled > 0, reason, rejected)

    def on_timer(self, now: float) -> None:
        """미체결 주문 정리 — 주기적으로 호출."""
        mono = time.monotonic()
        for o in list(self.orders.values()):
            if o.status != "ACK":
                continue
            if now - o.created_ts > self.timeout:
                o.status = "CANCELING"
                self._spawn(self._cancel(o))
            elif self.can_poll and mono - self.polled.get(o.id, mono - 1.5) >= 3.0:
                self.polled[o.id] = mono              # 접수 후 1.5초, 이후 3초마다 체결 여부 재확인
                self._spawn(self._poll(o))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _poll(self, o: Order) -> None:
        """체결통보가 누락됐을 때를 대비한 조회(실매매 브로커만)."""
        try:
            cum, avg, done = await self.broker.poll_order(o)
        except Exception:
            return
        if o.id not in self.orders:
            return
        if cum > o.filled and avg > 0:
            self._apply(o, cum, avg, self.engine.now)
        if done and o.id in self.orders:
            self._finish(o, "CANCELED", "증권사에서 종료된 주문")

    async def _cancel(self, o: Order) -> None:
        try:
            await self.broker.cancel_order(o)
        except Exception:
            # 이미 체결됐을 수 있다 — 체결통보를 기다리고, 계좌 대사가 최종 안전망
            if o.id in self.orders:
                o.status = "ACK"
                o.created_ts = self.engine.now
            return
        if o.id in self.orders:
            self._finish(o, "CANCELED", "시간 초과 취소")

    def working(self) -> list[dict]:
        return [{"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "filled": o.filled,
                 "price": o.limit_price, "status": o.status, "reason": o.reason}
                for o in self.orders.values()]

    async def drain(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)
