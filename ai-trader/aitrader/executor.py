"""주문 집행 에이전트 — 엔진의 주문 의도를 증권사로 보내고 체결을 엔진에 돌려준다.

submit() 은 동기 함수다: 엔진(핫패스)은 네트워크 왕복을 기다리지 않고 바로 다음 틱을 처리한다.

주문 한 건의 상태:
  SENT → ACK → FILLED | CANCELED | REJECTED
  SENT → UNKNOWN(전송 중 오류로 접수 여부 불명) → ACK | REJECTED | STUCK(끝내 확인 불가: 잠금 유지 + 정지)
엔진의 종목 잠금(pending)은 주문이 '확실히 끝났을 때'만 풀린다. 접수 여부를 모르는 주문의 잠금을
풀면 같은 주문이 두 번 나가거나 체결분이 장부에서 빠지기 때문이다.
"""
from __future__ import annotations

import asyncio
import time
from time import perf_counter_ns
from typing import Callable

from .agents.base import Agent
from .brokers.base import Broker, BrokerError
from .engine import Engine, LatencyStats
from .models import Fill, Order, OrderIntent

LIVE = ("SENT", "ACK", "UNKNOWN", "CANCELING")


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
        self.on_change: Callable[[], None] | None = None   # 주문 상태가 바뀔 때(실매매: 상태 저장)
        self.resolve_delays = (0.5, 1.0, 2.0, 4.0, 8.0)
        self._n = 0
        self._tasks: set = set()
        broker.on_fill = self.on_fill
        broker.on_reject = self.on_reject
        broker.on_poke = self.poke
        engine.execute = self.submit

    # ------------------------------------------------------------------ 전송
    def submit(self, it: OrderIntent) -> None:
        self.calls += 1
        self._n += 1
        o = Order(self._n, it.symbol, it.side, it.qty, it.limit_price, it.reason, it.ctx,
                  self.engine.now, status="SENT", sent_ns=perf_counter_ns())
        self.orders[o.id] = o
        self._spawn(self._place(o))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as e:
                self.engine.emit("system", "", f"상태 저장 실패(매매는 계속): {e!r}")

    async def _place(self, o: Order) -> None:
        try:
            await self.broker.place_order(o)
        except BrokerError as e:                     # 증권사가 명시적으로 거부 → 접수되지 않은 것이 확실
            self._finish(o, "REJECTED", str(e), rejected=True)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # 시간 초과·연결 끊김 — 접수됐는지 알 수 없다
            o.status = "UNKNOWN"
            o.error = repr(e)
            self.engine.emit("order", o.symbol, f"주문 응답 유실 — 접수 여부를 확인합니다: {e!r}")
            await self._resolve_unknown(o)
            return
        self._on_ack(o)

    def _on_ack(self, o: Order) -> None:
        o.rtt_ms = (perf_counter_ns() - o.sent_ns) / 1e6
        self.rtt.add(int(o.rtt_ms * 1000))
        if o.status in ("SENT", "UNKNOWN"):
            o.status = "ACK"
        self.by_bid[o.broker_id] = o
        self.polled[o.id] = time.monotonic() - 1.5   # 접수 1.5초 뒤 첫 재확인, 이후 3초마다
        self._changed()
        for args in self.early.pop(o.broker_id, ()):
            self.on_fill(*args)

    async def _resolve_unknown(self, o: Order) -> None:
        """접수 여부를 증권사에 물어 확정한다. 확정 전에는 종목 잠금을 풀지 않는다."""
        absent = 0
        for delay in self.resolve_delays:
            await asyncio.sleep(delay)
            if o.id not in self.orders:
                return
            try:
                state, bid = await self.broker.lookup_order(o)
            except Exception:
                state, bid = "unknown", ""
            if state == "found" and bid:
                o.broker_id = bid
                self.engine.emit("order", o.symbol, f"주문 접수 확인(주문번호 {bid})")
                self._on_ack(o)
                return
            if state == "absent":
                absent += 1
                if absent >= 2:                      # 두 번 연속 '없음' → 접수되지 않았다고 본다
                    self._finish(o, "REJECTED", "전송 실패(미접수 확인)")
                    return
            else:
                absent = 0
        o.status = "STUCK"
        msg = f"{o.symbol} 주문 접수 여부를 확인할 수 없습니다 — 계좌를 직접 확인한 뒤 [재개]를 누르세요"
        self.engine.risk.halt(msg, flatten=False)
        self.engine.emit("risk", o.symbol, msg)
        self._changed()

    # ------------------------------------------------------------------ 체결
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
                    self._changed()
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
        self.note(f"{o.symbol} {o.side} {qty}주 @ {self.engine.m.px(price)} ({o.rtt_ms:.1f}ms)", ts)
        try:
            self.engine.on_fill(Fill(o.symbol, o.side, qty, price, ts, o.id, o.reason, o.ctx))
        finally:                                      # 장부 반영 중 무슨 일이 있어도 주문은 끝낸다(잠금 누수 방지)
            if o.filled >= o.qty:
                self._finish(o, "FILLED")
            else:
                self._changed()

    def poke(self, broker_id: str) -> None:
        """증권사가 '이 주문에 변화가 있다'고 알려 왔다 — 수량·가격은 조회로 확인한다(통보 내용을 그대로 믿지 않는다)."""
        o = self.by_bid.get(broker_id)
        if o is not None and o.status == "ACK" and self.can_poll:
            self.polled[o.id] = time.monotonic()
            self._spawn(self._poll(o))

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
        self._changed()

    # ------------------------------------------------------------------ 주기 점검
    def on_timer(self, now: float) -> None:
        """미체결 주문 정리 — 주기적으로 호출."""
        mono = time.monotonic()
        for o in list(self.orders.values()):
            if o.status != "ACK":
                continue
            if now - o.created_ts > self.timeout:
                o.status = "CANCELING"
                self._spawn(self._cancel(o))
            elif self.can_poll and mono - self.polled.get(o.id, mono) >= 3.0:
                self.polled[o.id] = mono
                self._spawn(self._poll(o))

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
            # 이미 체결됐을 수 있다 — 통보/폴링으로 체결을 확인한다
            if o.id in self.orders:
                o.status = "ACK"
                o.created_ts = self.engine.now
                self.polled[o.id] = time.monotonic() - 3.0     # 다음 점검 때 바로 재확인
            return
        if o.id in self.orders:
            if self.can_poll:                         # 취소 직전에 체결된 수량이 있는지 한 번 더 본다
                try:
                    cum, avg, _ = await self.broker.poll_order(o)
                    if o.id in self.orders and cum > o.filled and avg > 0:
                        self._apply(o, cum, avg, self.engine.now)
                except Exception:
                    pass
            if o.id in self.orders:
                self._finish(o, "CANCELED", "시간 초과 취소")

    def working(self) -> list[dict]:
        return [{"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "filled": o.filled,
                 "price": o.limit_price, "status": o.status, "reason": o.reason}
                for o in self.orders.values()]

    def busy(self) -> bool:
        return bool(self.orders)

    def drop_stuck(self) -> list[str]:
        """사람이 계좌를 확인하고 [재개]를 눌렀을 때: 확인 불가 주문의 잠금을 푼다."""
        out = []
        for o in list(self.orders.values()):
            if o.status == "STUCK":
                out.append(o.symbol)
                self._finish(o, "CANCELED", "사람이 확인 후 해제")
        return out

    # ------------------------------------------------------------------ 종료·재시작
    def export(self) -> list[dict]:
        """재시작 후 이어받을 수 있게 진행 중 주문을 내보낸다."""
        return [{"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "limit_price": o.limit_price,
                 "reason": o.reason, "ctx": o.ctx, "created_ts": o.created_ts, "status": o.status,
                 "broker_id": o.broker_id, "broker_org": o.broker_org, "filled": o.filled}
                for o in self.orders.values()]

    async def recover(self, saved: list[dict]) -> int:
        """이전 실행이 남긴 주문을 정리한다: 꺼져 있는 동안 체결된 수량을 장부에 반영하고 잔량은 취소."""
        n = 0
        e = self.engine
        for d in saved:
            o = Order(0, d["symbol"], d["side"], int(d["qty"]), float(d.get("limit_price", 0.0)),
                      d.get("reason", ""), d.get("ctx") or {}, float(d.get("created_ts", 0.0)),
                      status="ACK", broker_id=str(d.get("broker_id", "")), broker_org=str(d.get("broker_org", "")),
                      filled=int(d.get("filled", 0)))
            if not o.broker_id:
                try:
                    state, bid = await self.broker.lookup_order(o)
                except Exception:
                    state, bid = "unknown", ""
                if state != "found":
                    if state == "unknown":
                        e.emit("risk", o.symbol, f"이전 실행의 {o.symbol} 주문 접수 여부 확인 불가 — 계좌 확인 필요")
                    continue
                o.broker_id = bid
            try:
                cum, avg, done = await self.broker.poll_order(o)
            except Exception as ex:
                e.emit("risk", o.symbol, f"이전 실행의 {o.symbol} 주문 상태 조회 실패: {ex!r}")
                continue
            if cum > o.filled and avg > 0:
                q = min(cum, o.qty) - o.filled
                e.symbol_state(o.symbol).last = e.symbol_state(o.symbol).last or avg
                e.on_fill(Fill(o.symbol, o.side, q, avg, time.time(), 0, o.reason, o.ctx))
                e.emit("order", o.symbol, f"꺼져 있는 동안 체결된 {o.side} {q}주를 장부에 반영")
                n += 1
            if not done:
                try:
                    await self.broker.cancel_order(o)
                    e.emit("order", o.symbol, f"이전 실행의 미체결 {o.side} 주문 취소")
                except Exception as ex:
                    e.emit("risk", o.symbol, f"이전 실행의 미체결 주문 취소 실패: {ex!r}")
        return n

    async def cancel_all(self, timeout: float = 5.0) -> None:
        """정상 종료 시: 걸려 있는 주문을 취소 요청한다(최선 노력)."""
        todo = [o for o in self.orders.values() if o.status == "ACK"]
        for o in todo:
            o.status = "CANCELING"
            self._spawn(self._cancel(o))
        await self.drain(timeout)

    async def drain(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)
