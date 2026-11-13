"""주문 집행 에이전트 — 엔진의 주문 의도를 증권사로 보내고 체결을 엔진에 돌려준다.

submit() 은 동기 함수다: 엔진(핫패스)은 네트워크 왕복을 기다리지 않고 바로 다음 틱을 처리한다.

주문 한 건의 상태:
  SENT → ACK → FILLED | CANCELED | REJECTED
  SENT → UNKNOWN(전송 중 오류로 접수 여부 불명) → ACK | REJECTED | STUCK(끝내 확인 불가: 잠금 유지 + 정지)
  ACK → CANCELING(취소 요청 → 최종 체결 수량 확정 중) → CANCELED | FILLED | STUCK
엔진의 종목 잠금(pending)은 주문이 '확실히 끝났을 때'만 풀린다. 접수 여부를 모르는 주문의 잠금을
풀면 같은 주문이 두 번 나가거나 체결분이 장부에서 빠지기 때문이다.

체결을 알게 되는 길은 둘이다: 실시간 통보(건별 수량·가격)와 조회(누적 수량·평균가). 두 길이 겹치거나
한쪽이 늦게 와도 같은 체결을 두 번 넣지 않도록 '주문별 누적 수량'을 기준으로 합친다.
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

    def __init__(self, broker: Broker, engine: Engine, timeout_sec: float = 10.0,
                 clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self.broker = broker
        self.engine = engine
        self.timeout = timeout_sec
        # 미체결 시간 초과를 재는 시계. 기본은 마지막 틱 시각이지만, 시세 시각이 실제 시각보다 늦을 수 있는
        # 실시간 모드에서는 총괄이 벽시계를 넘겨 준다(주문 제안이 뜨자마자 만료되는 일을 막는다).
        self.clock: Callable[[], float] = clock or (lambda: engine.now)
        self.orders: dict[int, Order] = {}            # 진행 중 주문
        self.by_bid: dict[str, Order] = {}
        self.early: dict[str, list] = {}              # 주문 응답보다 먼저 도착한 체결통보
        self.ws_cum: dict[int, int] = {}              # 주문별 '통보로 받은' 누적 체결수량
        self.polled: dict[int, float] = {}            # 주문별 마지막 폴링 시각
        self.can_poll = bool(getattr(broker, "supports_poll", False))
        self.recent: dict[str, Order] = {}            # 방금 끝난 주문(취소 직후 도착하는 늦은 체결 대비)
        self.late_cum: dict[str, int] = {}            # 끝난 주문별 '통보로 받은' 누적 체결수량(이중 반영 방지)
        self.settle_delays = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)   # 취소 접수 뒤 최종 체결 수량을 확인하는 조회 간격
        self.follow_delays = (5.0, 20.0, 60.0)        # 끝낸 주문을 더 지켜보는 조회 간격(뒤늦게 나타나는 체결)
        self._bg: set = set()                         # 추적 조회 작업(종료를 기다리지 않는다)
        self._polling: set[int] = set()               # 조회가 진행 중인 주문(느린 조회가 겹쳐 쌓이지 않게)
        self.rtt = LatencyStats(1024)                 # 주문 전송→접수 응답(ms×1000)
        self.on_change: Callable[[], None] | None = None   # 주문 상태가 바뀔 때(실매매: 상태 저장)
        self.resolve_delays = (0.5, 1.0, 2.0, 4.0, 8.0)
        self._n = 0
        self._tasks: set = set()
        broker.on_fill = self.on_fill
        broker.on_reject = self.on_reject
        broker.on_poke = self.poke
        broker.on_cancel = self.on_cancel
        engine.execute = self.submit

    # ------------------------------------------------------------------ 전송
    def submit(self, it: OrderIntent) -> None:
        self.calls += 1
        self._n += 1
        o = Order(self._n, it.symbol, it.side, it.qty, it.limit_price, it.reason, it.ctx,
                  self.clock(), status="SENT", sent_ns=perf_counter_ns())
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
            if late is not None:
                # 끝난 주문에 뒤늦게 온 통보 — 장부에는 반영해야 하지만, 취소 직후 조회로 이미 넣은 체결의
                # 통보일 수 있다. 통보 누적 수량이 '이미 반영한 수량'을 넘는 만큼만 넣는다.
                cum = self.late_cum.get(broker_id, 0) + qty
                self.late_cum[broker_id] = cum
                self._late(late, cum, price, ts)
                return
            self.early.setdefault(broker_id, []).append((broker_id, qty, price, ts))
            if len(self.early) > 200:                 # 봇이 낸 주문이 아닌 통보(수동 주문 등)는 버린다
                self.early.pop(next(iter(self.early)))
            return
        cum = self.ws_cum.get(o.id, 0) + qty
        self.ws_cum[o.id] = cum
        self._apply(o, cum, price, ts)

    @staticmethod
    def _inc_price(o: Order, total: int, avg: float) -> float:
        """조회가 주는 가격은 '누적 평균'이다. 이번에 늘어난 수량의 가격을 역산한다(총 체결금액이 맞도록)."""
        qty = total - o.filled
        if o.filled > 0 and o.avg_price > 0 and qty > 0:
            inc = (avg * total - o.avg_price * o.filled) / qty
            if inc > 0:
                return inc
        return avg

    def _late(self, o: Order, cum: int, price: float, ts: float, avg: bool = False) -> int:
        """끝난 주문의 뒤늦은 체결을 장부에 반영한다(누적 기준). 반영한 수량을 돌려준다."""
        total = min(cum, o.qty)
        qty = total - o.filled
        if qty <= 0:
            return 0
        if avg:
            price = self._inc_price(o, total, price)
        o.avg_price = (o.avg_price * o.filled + price * qty) / (o.filled + qty)
        o.filled += qty
        self.engine.on_fill(Fill(o.symbol, o.side, qty, price, ts, o.id, o.reason, o.ctx))
        self._changed()
        return qty

    def _apply(self, o: Order, cum: int, price: float, ts: float, avg: bool = False) -> None:
        """avg=True: price 가 누적 평균가(조회 결과). False: 이번 체결분의 가격(실시간 통보)."""
        total = min(cum, o.qty)
        qty = total - o.filled
        if qty <= 0:
            return
        if avg:
            price = self._inc_price(o, total, price)
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
        if o is not None:
            if o.status == "ACK" and self.can_poll:
                self.polled[o.id] = time.monotonic()
                self._spawn(self._poll(o))
            return
        late = self.recent.get(broker_id)
        if late is not None and self.can_poll:        # 이미 끝낸 주문에 변화 통보 → 뒤늦은 체결일 수 있으니 바로 조회
            self._follow_up(late, once=True)

    def on_cancel(self, broker_id: str) -> None:
        """증권사·사람 쪽에서 취소된 주문(거부가 아니므로 '거부 반복' 정지 횟수에 넣지 않는다)."""
        o = self.by_bid.get(broker_id)
        if o is not None and o.status in ("SENT", "ACK", "CANCELING"):
            self._finish(o, "CANCELED", "취소됨")

    def on_reject(self, broker_id: str, msg: str) -> None:
        o = self.by_bid.get(broker_id)
        if o is not None and o.status in ("SENT", "ACK"):
            self._finish(o, "REJECTED", msg, rejected=True)

    def _finish(self, o: Order, status: str, reason: str = "", rejected: bool = False) -> None:
        o.status = status
        o.error = reason
        self.orders.pop(o.id, None)
        self.by_bid.pop(o.broker_id, None)
        ws = self.ws_cum.pop(o.id, 0)
        self.polled.pop(o.id, None)
        if o.broker_id and o.filled < o.qty:
            self.recent[o.broker_id] = o
            self.late_cum[o.broker_id] = ws
            if len(self.recent) > 100:
                self.late_cum.pop(next(iter(self.recent)), None)
                self.recent.pop(next(iter(self.recent)))
            if self.can_poll and status == "CANCELED":
                self._follow_up(o)
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
            elif self.can_poll and o.id not in self._polling and mono - self.polled.get(o.id, mono) >= 3.0:
                self.polled[o.id] = mono
                self._spawn(self._poll(o))

    async def _poll(self, o: Order) -> None:
        """체결통보가 누락됐을 때를 대비한 조회(실매매 브로커만)."""
        if o.id in self._polling:
            return
        self._polling.add(o.id)
        try:
            cum, avg, done = await self.broker.poll_order(o)
        except Exception:
            return
        finally:
            self._polling.discard(o.id)
        if o.id not in self.orders or o.status != "ACK":
            return                                    # 그 사이 취소 절차가 시작됐으면 그쪽에서 확정한다
        if cum > o.filled and avg > 0:
            self._apply(o, cum, avg, self.engine.now, avg=True)
        if done and o.id in self.orders:
            self._finish(o, "CANCELED", "증권사에서 종료된 주문")

    async def _cancel(self, o: Order) -> None:
        try:
            await self.broker.cancel_order(o)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 이미 체결됐을 수 있다 — 통보/폴링으로 체결을 확인한다
            if o.id in self.orders:
                o.status = "ACK"
                o.created_ts = self.clock()
                self.polled[o.id] = time.monotonic() - 3.0     # 다음 점검 때 바로 재확인
            return
        if o.id not in self.orders:
            return
        if not self.can_poll:
            self._finish(o, "CANCELED", "시간 초과 취소")
            return
        await self._settle(o, "시간 초과 취소")

    async def _settle(self, o: Order, reason: str) -> None:
        """취소 요청이 접수된 뒤, 최종 체결 수량을 조회로 확정한다.

        취소 '접수'와 실제 취소 사이에 체결될 수 있고, 조회 한 번이 실패했다고 그 체결분을 버리면
        계좌에는 있는데 장부에는 없는 주식이 생긴다. 그래서 (1) 증권사가 끝난 주문이라고 확인해 주거나
        (2) 연속 두 번 같은 수량이 조회될 때까지 기다리고, 끝내 조회가 안 되면 잠금을 풀지 않고 사람을 부른다."""
        ok = same = 0
        last_cum = -1
        for delay in self.settle_delays:
            if delay:
                await asyncio.sleep(delay)
            if o.id not in self.orders:
                return
            try:
                cum, avg, done = await self.broker.poll_order(o)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if o.id not in self.orders:
                return
            ok += 1
            if cum > o.filled and avg > 0:
                self._apply(o, cum, avg, self.engine.now, avg=True)
                if o.id not in self.orders:
                    return                            # 그 사이 전량 체결돼 끝났다
            if done:
                self._finish(o, "CANCELED", reason)
                return
            same = same + 1 if cum == last_cum else 1
            last_cum = cum
            if ok >= 2 and same >= 2:
                break
        if o.id not in self.orders:
            return
        if ok >= 2 and same >= 2:
            # '끝남' 표시는 못 받았지만 체결 수량이 더 변하지 않는다 → 취소된 것으로 보고, 뒤늦은 체결은 추적 조회로 잡는다
            self._finish(o, "CANCELED", reason)
            return
        o.status = "STUCK"
        msg = f"{o.symbol} 주문의 최종 체결 수량을 확인할 수 없습니다 — 계좌를 직접 확인한 뒤 [재개]를 누르세요"
        self.engine.risk.halt(msg, flatten=False)
        self.engine.emit("risk", o.symbol, msg)
        self._changed()

    def _follow_up(self, o: Order, once: bool = False) -> None:
        t = asyncio.get_running_loop().create_task(self._follow(o, (0.0,) if once else self.follow_delays))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _follow(self, o: Order, delays: tuple) -> None:
        """취소로 끝낸 주문을 얼마간 더 조회한다 — 취소 처리 직전에 체결된 수량이 뒤늦게 나타나는 경우를 잡는다."""
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            if self.recent.get(o.broker_id) is not o:
                return
            try:
                cum, avg, _ = await self.broker.poll_order(o)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if cum > o.filled and avg > 0:
                q = self._late(o, cum, avg, self.clock(), avg=True)
                if q:
                    self.engine.emit("order", o.symbol, f"취소 처리 뒤에 확인된 {o.side} 체결 {q}주를 장부에 반영")

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
                 "broker_id": o.broker_id, "broker_org": o.broker_org, "filled": o.filled,
                 "avg_price": o.avg_price}
                for o in self.orders.values()]

    async def recover(self, saved: list[dict]) -> int:
        """이전 실행이 남긴 주문을 정리한다: 꺼져 있는 동안 체결된 수량을 장부에 반영하고 잔량은 취소."""
        n = 0
        e = self.engine
        for d in saved:
            o = Order(0, d["symbol"], d["side"], int(d["qty"]), float(d.get("limit_price", 0.0)),
                      d.get("reason", ""), d.get("ctx") or {}, float(d.get("created_ts", 0.0)),
                      status="ACK", broker_id=str(d.get("broker_id", "")), broker_org=str(d.get("broker_org", "")),
                      filled=int(d.get("filled", 0)), avg_price=float(d.get("avg_price", 0.0)))
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
                e.emit("risk", o.symbol, f"이전 실행의 {o.symbol} 주문 상태 조회 실패: {ex!r} — 계좌 확인 필요")
                continue
            n += self._recovered(o, cum, avg)
            if not done:
                try:
                    await self.broker.cancel_order(o)
                    e.emit("order", o.symbol, f"이전 실행의 미체결 {o.side} 주문 취소")
                except Exception as ex:
                    e.emit("risk", o.symbol, f"이전 실행의 미체결 주문 취소 실패: {ex!r}")
                for delay in self.settle_delays[1:4]:  # 취소 직전에 더 체결된 수량이 있는지 다시 본다
                    await asyncio.sleep(delay)
                    try:
                        cum, avg, done = await self.broker.poll_order(o)
                    except Exception:
                        continue
                    n += self._recovered(o, cum, avg)
                    if done:
                        break
        return n

    def _recovered(self, o: Order, cum: int, avg: float) -> int:
        """재시작 정리 중 확인된 체결(누적)을 장부에 반영. 반영했으면 1."""
        total = min(cum, o.qty)
        q = total - o.filled
        if q <= 0 or avg <= 0:
            return 0
        e = self.engine
        px = self._inc_price(o, total, avg)
        s = e.symbol_state(o.symbol)
        s.last = s.last or px
        o.avg_price = (o.avg_price * o.filled + px * q) / (o.filled + q)
        o.filled += q
        e.on_fill(Fill(o.symbol, o.side, q, px, time.time(), 0, o.reason, o.ctx))
        e.emit("order", o.symbol, f"꺼져 있는 동안 체결된 {o.side} {q}주를 장부에 반영")
        return 1

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

    def shutdown(self) -> None:
        """종료 시: 아직 돌고 있는 작업(취소 확정 대기·추적 조회)을 멈춘다.

        증권사 연결을 닫은 뒤에도 확정 대기가 계속 돌면, 조회 실패를 '확인 불가'로 오인해 정지 상태를
        저장해 버린다. 확정하지 못한 주문은 진행 중 주문으로 저장되므로 다음 시작 때 recover 가 정리한다."""
        for t in list(self._tasks) + list(self._bg):
            t.cancel()
