"""주문 집행 에이전트 — 엔진의 주문 의도를 증권사로 보내고 체결을 엔진에 돌려준다.

submit() 은 동기 함수다: 엔진(핫패스)은 네트워크 왕복을 기다리지 않고 바로 다음 틱을 처리한다.

주문 한 건의 상태:
  SENT → ACK → FILLED | CANCELED | REJECTED
  SENT → UNKNOWN(전송 중 오류로 접수 여부 불명) → ACK | REJECTED | STUCK(끝내 확인 불가: 잠금 유지 + 정지)
  ACK → CANCELING(취소 요청 → 최종 체결 수량 확정 중) → CANCELED | FILLED | STUCK
  취소 요청이 실패하면(거부든 응답 없음이든) '끝난 주문'이라고 단정하지 않는다. 조회로 끝남 표시나 전량 체결이
  확인되면 거기서 끝내고, 아니면 다시 시도하다가 세 번 연달아 실패하면 STUCK(끝없이 되풀이하지 않는다).
  STUCK 주문은 가끔 다시 조회한다: 일시 장애였다면 조회가 되는 대로 체결을 반영하고, 끝난 주문이면 잠금을 푼다.
  아직 살아 있으면 취소를 다시 시도하고, 그 주문의 장이 끝났으면(당일 주문은 소멸) 끝난 것으로 정리한다
  (신규 매수 정지는 사람이 [재개]를 누를 때까지 그대로다).
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
MAX_CANCEL_FAILS = 3          # 체결·종료가 확인되지 않은 채 취소 요청이 이만큼 연달아 실패하면 사람을 부른다
SETTLE_SAME = 3               # 취소 뒤 '끝남' 표시가 없을 때: 조회한 체결 수량이 이만큼 연달아 같아야 끝난 것으로 본다


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
        self.wall: Callable[[], float] = time.time    # 실제 시각(주문 내역의 접수 시각·장 마감 여부를 따질 때. 시세 시각과 무관)
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
        self._following: dict[str, Order] = {}        # 추적 조회가 아직 끝나지 않은 주문(이대로 꺼지면 다음 시작 때 한 번 더 본다)
        self._cfail: dict[int, tuple[int, int]] = {}  # 주문별 (연속 취소 실패 횟수, 그때의 체결 수량)
        self._polling: set[int] = set()               # 조회가 진행 중인 주문(느린 조회가 겹쳐 쌓이지 않게)
        self.rtt = LatencyStats(1024)                 # 주문 전송→접수 응답(ms×1000)
        self.on_change: Callable[[], None] | None = None   # 주문 상태가 바뀔 때(실매매: 상태 저장)
        self.resolve_delays = (0.5, 1.0, 2.0, 4.0, 8.0)
        self.stuck_poll_sec = 30.0                    # 확인 불가 주문을 다시 물어보는 간격
        self._absent: dict[int, int] = {}             # 확인 불가 주문별 '증권사에 없음' 연속 횟수
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
                  self.clock(), status="SENT", sent_ns=perf_counter_ns(), wall_ts=self.wall())
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
        self._cfail.pop(o.id, None)
        self._absent.pop(o.id, None)
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
            if o.status == "STUCK":
                if (self.can_poll and o.id not in self._polling
                        and mono - self.polled.get(o.id, -1e9) >= self.stuck_poll_sec):
                    self.polled[o.id] = mono
                    self._spawn(self._recheck(o))
                continue
            if o.status != "ACK":
                continue
            if now - (o.timer_ts or o.created_ts) > self.timeout:
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

    async def _recheck(self, o: Order) -> None:
        """확인 불가(STUCK) 주문을 다시 물어본다. 조회가 되면 체결을 반영하고, 증권사가 끝난 주문이라고 하면 잠금을 푼다.
        주문번호조차 모르는 주문은 접수 여부부터 다시 찾는다. 정지 상태는 건드리지 않는다(사람이 보고 [재개])."""
        if o.id in self._polling:
            return
        self._polling.add(o.id)
        try:
            if not o.broker_id:
                state, bid = await self.broker.lookup_order(o)
                if o.id not in self.orders or o.status != "STUCK":
                    return
                if state == "found" and bid:
                    o.broker_id = bid
                    self.by_bid[bid] = o
                    self.ws_cum.setdefault(o.id, o.filled)
                    self._absent.pop(o.id, None)
                    self.polled[o.id] = -1e9               # 다음 점검 때 바로 체결 여부를 본다
                    self.engine.emit("order", o.symbol, f"확인 불가였던 주문의 접수를 확인(주문번호 {bid})")
                    self._changed()
                elif state == "absent":
                    n = self._absent.get(o.id, 0) + 1
                    self._absent[o.id] = n
                    if n >= 2:                             # 두 번 연속 '없음' → 접수되지 않은 주문
                        self._finish(o, "REJECTED", "전송 실패(미접수 확인)")
                else:
                    self._absent.pop(o.id, None)
                return
            cum, avg, done = await self.broker.poll_order(o)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            self._polling.discard(o.id)
        if o.id not in self.orders or o.status != "STUCK" or cum < o.filled:
            return
        if cum > o.filled and avg > 0:
            self._apply(o, cum, avg, self.clock(), avg=True)
        if o.id not in self.orders:
            return                                    # 전량 체결로 끝났다
        if done or self._session_over(o):
            # 증권사가 끝난 주문이라고 했거나, 그 주문을 낸 날의 장이 끝났다(당일 주문은 장 마감과 함께 소멸한다)
            self._finish(o, "CANCELED", "뒤늦게 종료 확인" if done else "장 마감으로 소멸")
            self.engine.emit("order", o.symbol, f"확인 불가였던 {o.symbol} 주문이 끝난 것을 확인했습니다 — 종목 잠금 해제")
            return
        # 조회는 되는데 아직 살아 있는 주문이다 → 원래 하려던 취소를 다시 보낸다(호출 한도 등 일시적 사유로 막혔던 경우)
        try:
            await self.broker.cancel_order(o)
        except asyncio.CancelledError:
            raise
        except Exception:
            return                                    # 여전히 안 된다 — 확인 불가 상태로 둔다(다음 주기에 다시)
        if o.id in self.orders and o.status == "STUCK":
            o.status = "CANCELING"
            self.engine.emit("order", o.symbol, f"확인 불가였던 {o.symbol} 주문의 취소가 접수됐습니다")
            await self._settle(o, "시간 초과 취소")

    def _session_over(self, o: Order) -> bool:
        """그 주문을 낸 날의 정규장이 끝났는가(실제 시각 기준). 당일 주문만 쓰므로, 끝났다면 주문은 더 체결될 수 없다."""
        if o.wall_ts <= 0:
            return False
        e = self.engine
        now = self.wall()
        if e.m.local_day(now) > e.m.local_day(o.wall_ts):
            return True
        return (now + e.m.utc_offset(now)) % 86400.0 >= e.close_tod

    async def _cancel(self, o: Order) -> None:
        try:
            await self.broker.cancel_order(o)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            if o.id not in self.orders:
                return
            if isinstance(ex, BrokerError) and self.can_poll:
                # 증권사가 취소를 명시적으로 거부했다. 이미 끝난 주문일 수 있다(취소보다 먼저 전량 체결됐거나, 앞서 보낸
                # 취소가 이미 처리됨) — 조회로 확인해 끝남 표시나 전량 체결이 보이면 거기서 끝낸다. 하지만 거부 사유는
                # 그것만이 아니다(호출 한도, 주문이 아직 거래소로 가는 중 등). 조회에 아직 열려 있다고 나오면 '끝났다'고
                # 단정하지 않는다 — 잠금을 풀면 살아 있는 주문 위에 새 주문이 나가 수량이 두 배가 된다.
                if await self._confirm_ended(o, "취소 거부(이미 끝난 주문)"):
                    return
                if o.id not in self.orders:
                    return
            self._cancel_failed(o, ex)
            return
        if o.id not in self.orders:
            return
        if not self.can_poll:
            self._finish(o, "CANCELED", "시간 초과 취소")
            return
        await self._settle(o, "시간 초과 취소")

    async def _confirm_ended(self, o: Order, reason: str) -> bool:
        """취소가 거부된 주문이 이미 끝난 것인지 조회로 확인한다. 체결이 보이면 반영하고, 증권사가 끝난 주문이라고 하면
        끝낸다. 끝났으면(전량 체결 포함) True. 아직 열려 있거나 조회가 안 되면 False — 호출한 쪽이 취소 실패로 센다."""
        for delay in self.settle_delays[:4]:
            if delay:
                await asyncio.sleep(delay)
            if o.id not in self.orders:
                return True
            try:
                cum, avg, done = await self.broker.poll_order(o)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if o.id not in self.orders:
                return True
            if cum < o.filled:
                continue
            if cum > o.filled and avg > 0:
                self._apply(o, cum, avg, self.engine.now, avg=True)
                if o.id not in self.orders:
                    return True                        # 전량 체결로 끝났다
            if done:
                self._finish(o, "CANCELED", reason)
                return True
        return False

    def _cancel_failed(self, o: Order, ex: Exception) -> None:
        """취소 요청이 실패했고 주문이 끝났다는 확인도 없다 → 다음 시간 초과 때 다시 시도한다. 체결도 종료도 확인되지 않은 채
        세 번 연달아 실패하면 끝없이 되풀이하지 않고(종목 잠금·계좌 대사 중단이 조용히 계속된다) 사람을 부른다."""
        n, at = self._cfail.get(o.id, (0, -1))
        n = n + 1 if at == o.filled else 1             # 그 사이 체결이 늘었으면 처음부터 다시 센다
        self._cfail[o.id] = (n, o.filled)
        if n >= MAX_CANCEL_FAILS:
            o.status = "STUCK"
            msg = (f"{o.symbol} 주문의 취소가 계속 실패합니다({ex!r}) — 이미 끝난 주문일 수 있습니다. "
                   f"계좌를 직접 확인한 뒤 [재개]를 누르세요")[:240]
            self.engine.risk.halt(msg, flatten=False)
            self.engine.emit("risk", o.symbol, msg)
            self._changed()
            return
        o.status = "ACK"
        o.timer_ts = self.clock()                      # 시간 초과를 다시 잰다(주문을 낸 시각 created_ts 는 그대로 둔다)
        self.polled[o.id] = time.monotonic() - 3.0     # 다음 점검 때 바로 재확인

    async def _settle(self, o: Order, reason: str, need: int = SETTLE_SAME) -> None:
        """취소 요청이 접수된(또는 '취소할 것이 없다'고 거부된) 뒤, 최종 체결 수량을 조회로 확정한다.
        need: '끝남' 표시가 없을 때, 같은 수량이 연달아 몇 번 조회돼야 끝난 것으로 볼지.

        취소 '접수'와 실제 취소 사이에 체결될 수 있고, 조회 한 번이 실패했다고 그 체결분을 버리면
        계좌에는 있는데 장부에는 없는 주식이 생긴다. 그래서 (1) 증권사가 끝난 주문이라고 확인해 주거나
        (2) 연속 세 번 같은 수량이 조회될 때까지 기다리고, 끝내 조회가 안 되면 잠금을 풀지 않고 사람을 부른다.
        (두 번이면 취소 접수 0.5초 뒤에 끝내게 되는데, 그때는 주문이 아직 살아 있을 수 있다 — 그 사이의 체결을
         모른 채 잠금을 풀면 엔진이 낡은 수량으로 다음 주문을 낸다.)"""
        same = 0
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
            if cum < o.filled:
                continue                              # 이미 반영한 수량보다 적다 — 이 주문의 최신 상태가 아니다(확인으로 세지 않는다)
            if cum > o.filled and avg > 0:
                self._apply(o, cum, avg, self.engine.now, avg=True)
                if o.id not in self.orders:
                    return                            # 그 사이 전량 체결돼 끝났다
            if done:
                self._finish(o, "CANCELED", reason)
                return
            same = same + 1 if cum == last_cum else 1
            last_cum = cum
            if same >= need:
                break
        if o.id not in self.orders:
            return
        if same >= need:
            # '끝남' 표시는 못 받았지만 체결 수량이 더 변하지 않는다 → 취소된 것으로 보고, 뒤늦은 체결은 추적 조회로 잡는다
            self._finish(o, "CANCELED", reason)
            return
        o.status = "STUCK"
        msg = f"{o.symbol} 주문의 최종 체결 수량을 확인할 수 없습니다 — 계좌를 직접 확인한 뒤 [재개]를 누르세요"
        self.engine.risk.halt(msg, flatten=False)
        self.engine.emit("risk", o.symbol, msg)
        self._changed()

    def _follow_up(self, o: Order, once: bool = False) -> None:
        if not once:
            self._following[o.broker_id] = o
        t = asyncio.get_running_loop().create_task(self._follow(o, (0.0,) if once else self.follow_delays, not once))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _follow(self, o: Order, delays: tuple, tracked: bool = False) -> None:
        """취소로 끝낸 주문을 얼마간 더 조회한다 — 취소 처리 직전에 체결된 수량이 뒤늦게 나타나는 경우를 잡는다.
        끝까지 돌았으면 추적 목록에서 뺀다. 도중에 프로그램이 꺼지면(작업 취소) 목록에 남아 저장되고,
        다음 시작 때 recover 가 한 번 더 조회한다."""
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            if self.recent.get(o.broker_id) is not o:
                break
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
        if tracked and self._following.get(o.broker_id) is o:
            del self._following[o.broker_id]
            self._changed()

    def working(self) -> list[dict]:
        return [{"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "filled": o.filled,
                 "price": o.limit_price, "status": o.status, "reason": o.reason}
                for o in self.orders.values()]

    def busy(self) -> bool:
        return bool(self.orders)

    def unsettled(self) -> set[str]:
        """체결 수량이 아직 확정되지 않은 주문이 걸린 종목: 진행 중·확인 불가 주문, 그리고 취소로 끝냈지만 뒤늦은 체결을
        지켜보는 중인 주문. 이 종목의 장부·계좌 차이는 그 주문의 체결일 수 있으므로 계좌 대사는 확정될 때까지 미룬다
        (먼저 '판 것'으로 정리해 버리면, 나중에 그 주문의 체결이 확인될 때 같은 수량을 한 번 더 반영하게 된다)."""
        return {o.symbol for o in self.orders.values()} | {o.symbol for o in self._following.values()}

    def drop_stuck(self) -> list[str]:
        """사람이 계좌를 확인하고 [재개]를 눌렀을 때: 확인 불가 주문의 잠금을 푼다."""
        out = []
        for o in list(self.orders.values()):
            if o.status == "STUCK":
                out.append(o.symbol)
                self._finish(o, "CANCELED", "사람이 확인 후 해제")
        return out

    # ------------------------------------------------------------------ 종료·재시작
    @staticmethod
    def _dump(o: Order, status: str) -> dict:
        return {"id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "limit_price": o.limit_price,
                "reason": o.reason, "ctx": o.ctx, "created_ts": o.created_ts, "status": status,
                "broker_id": o.broker_id, "broker_org": o.broker_org, "filled": o.filled,
                "avg_price": o.avg_price, "wall_ts": o.wall_ts}

    def export(self) -> list[dict]:
        """재시작 후 이어받을 수 있게 주문을 내보낸다: 진행 중인 주문, 그리고 취소로 끝냈지만 뒤늦은 체결을
        아직 지켜보는 중인 주문(status=CANCELED — 다음 시작 때 한 번 더 조회만 한다)."""
        live = [self._dump(o, o.status) for o in self.orders.values()]
        return live + [self._dump(o, "CANCELED") for o in self._following.values() if o.filled < o.qty]

    async def _poll_retry(self, o: Order):
        """재시작 정리용 조회: 한 번 실패했다고 포기하지 않는다. 끝내 안 되면 None."""
        for delay in self.settle_delays[:4]:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.broker.poll_order(o)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                o.error = repr(ex)
        return None

    async def _lookup_retry(self, o: Order) -> tuple[str, str]:
        """주문번호 없이 저장된 주문(전송 도중 꺼짐)의 접수 여부: 'found' | 'absent'(두 번 연속 없음) | 'unknown'."""
        absent = 0
        for delay in self.settle_delays[:5]:
            if delay:
                await asyncio.sleep(delay)
            try:
                state, bid = await self.broker.lookup_order(o)
            except asyncio.CancelledError:
                raise
            except Exception:
                state, bid = "unknown", ""
            if state == "found" and bid:
                return "found", bid
            absent = absent + 1 if state == "absent" else 0
            if absent >= 2:
                return "absent", ""
        return "unknown", ""

    def _keep_stuck(self, o: Order, why: str) -> None:
        """이전 실행의 주문이 끝났는지 확인하지 못했다 → 잊지 않는다. 확인 불가 주문으로 남겨 종목을 잠그고
        신규 매수를 멈춘다(저장도 계속된다 → 다음 시작 때 다시 확인, 사람이 [재개]를 누르면 해제).

        이후에 오는 체결 통보(건별)는 '지금까지 장부에 넣은 수량' 다음부터 센다. 재시작 정리는 실시간 통보 연결보다
        먼저 끝나고, 꺼져 있던 동안의 통보는 다시 오지 않는다 — 여기서 조회로 넣은 체결의 통보가 뒤따라 올 일이 없다."""
        e = self.engine
        self._n += 1
        o.id = self._n
        o.status = "STUCK"
        self.orders[o.id] = o
        self.ws_cum[o.id] = o.filled
        if o.broker_id:
            self.by_bid[o.broker_id] = o               # 뒤늦게 오는 체결 통보는 이 주문으로 반영된다
        e.pending.setdefault(o.symbol, OrderIntent(o.symbol, o.side, o.qty, o.limit_price, o.reason, o.ctx))
        msg = f"이전 실행의 {o.symbol} {o.side} {o.qty}주 주문: {why} — 계좌를 직접 확인한 뒤 [재개]를 누르세요"
        e.risk.halt(msg, flatten=False)
        e.emit("risk", o.symbol, msg)

    def _remember(self, o: Order, follow: bool) -> None:
        """재시작 정리에서 끝낸 주문을 '방금 끝난 주문'처럼 기억한다 — 뒤늦게 오는 체결 통보를 받고,
        follow 면 얼마간 더 조회한다(취소 접수와 실제 취소 사이에 체결된 수량). 통보는 _keep_stuck 과 같은 기준으로 센다."""
        if not o.broker_id or o.filled >= o.qty:
            return
        o.status = "CANCELED"
        self.recent[o.broker_id] = o
        self.late_cum[o.broker_id] = o.filled
        if follow and self.can_poll:
            self._follow_up(o)

    async def recover(self, saved: list[dict]) -> int:
        """이전 실행이 남긴 주문을 정리한다: 꺼져 있는 동안 체결된 수량을 장부에 반영하고 잔량은 취소.

        끝났다고 확인하지 못한 주문은 잊지 않는다(_keep_stuck). 조회 한 번이 실패했다고 잊어버리면, 그 주문이
        나중에 체결됐을 때 계좌에는 있고 장부에는 없는 주식이 생기고 아무도 알려 주지 않는다."""
        n = 0
        e = self.engine
        for d in saved:
            o = Order(0, d["symbol"], d["side"], int(d["qty"]), float(d.get("limit_price", 0.0)),
                      d.get("reason", ""), d.get("ctx") or {}, float(d.get("created_ts", 0.0)),
                      status="ACK", broker_id=str(d.get("broker_id", "")), broker_org=str(d.get("broker_org", "")),
                      filled=int(d.get("filled", 0)), avg_price=float(d.get("avg_price", 0.0)),
                      wall_ts=float(d.get("wall_ts") or d.get("created_ts") or 0.0))
            if d.get("status") == "CANCELED":
                # 취소로 끝낸 뒤 뒤늦은 체결을 지켜보던 주문 — 다시 조회해 그 사이 체결이 있었으면 반영한다.
                got = await self._poll_retry(o) if o.broker_id else None
                if got is None:
                    e.emit("risk", o.symbol, f"이전 실행에서 취소한 {o.symbol} 주문의 최종 체결 수량을 다시 확인하지 못했습니다 — 계좌 확인 필요")
                    self._remember(o, True)                # 얼마간 더 조회해 본다(그동안 이 종목의 계좌 대사는 미룬다)
                    continue
                n += self._recovered(o, got[0], got[1])
                if got[2] or o.filled >= o.qty:
                    continue                               # 증권사가 끝난 주문이라고 확인했다
                # 끝났다는 표시가 없다 → 아직 살아 있을 수 있다. 아래에서 살아 있는 주문과 똑같이 취소·확인한다.
                cum, avg, done = got
            else:
                cum = avg = done = None
            if cum is None:
                if not o.broker_id:
                    state, bid = await self._lookup_retry(o)
                    if state == "absent":
                        continue                       # 증권사에 닿지 않은 주문
                    if state != "found":
                        self._keep_stuck(o, "접수 여부를 확인할 수 없습니다")
                        continue
                    o.broker_id = bid
                got = await self._poll_retry(o)
                if got is None:
                    self._keep_stuck(o, f"상태를 조회할 수 없습니다({o.error})"[:120])
                    continue
                cum, avg, done = got
                n += self._recovered(o, cum, avg)
                if done or o.filled >= o.qty:
                    continue
            # 그 주문을 낸 날의 장이 이미 끝났다면 당일 주문이라 소멸했을 것이다. 그래도 취소는 한 번 보내 보고(살아 있다면
            # 꼭 필요하다), 취소가 안 되고 끝났다는 표시가 없어도 '확인 불가'로 멈추지는 않는다.
            expired = self._session_over(o) if o.wall_ts > 0 else False
            canceled = True
            try:
                await self.broker.cancel_order(o)
                e.emit("order", o.symbol, f"이전 실행의 미체결 {o.side} 주문 취소")
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # 거부든 응답 없음이든 '취소됐다'는 증거가 아니다(거부 사유가 '취소할 것이 없음'이 아닐 수도 있다).
                # 아래 조회에서 증권사가 끝난 주문이라고 해야 끝낸다(지난 날의 주문은 예외 — 이미 소멸했다).
                canceled = False
                e.emit("order" if isinstance(ex, BrokerError) else "risk", o.symbol,
                       f"이전 실행의 미체결 {o.side} 주문 취소가 되지 않았습니다: {ex!r}"[:180])
            ended = confirmed = False
            same = 0
            last = -1
            for delay in self.settle_delays[1:]:       # 취소 직전에 더 체결된 수량이 있는지, 정말 끝났는지 다시 본다
                await asyncio.sleep(delay)
                try:
                    cum, avg, done = await self.broker.poll_order(o)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if cum < o.filled:
                    continue
                n += self._recovered(o, cum, avg)
                if done or o.filled >= o.qty:
                    ended = confirmed = True
                    break
                same = same + 1 if cum == last else 1
                last = cum
                if (canceled and same >= SETTLE_SAME) or (expired and not canceled):
                    ended = True                       # 취소는 접수됐고 체결 수량이 더 변하지 않는다 / 이미 소멸한 지난 날의 주문
                    break
            if not ended:
                self._keep_stuck(o, "취소됐는지 확인할 수 없습니다")
            elif not confirmed:
                self._remember(o, True)                # '끝남' 표시는 못 받았다 → 뒤늦은 체결을 얼마간 더 지켜본다
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
