"""실시간 총괄 대시보드 서버 (aiohttp).

  GET  /            대시보드 화면
  GET  /ws          실시간 상태 푸시(WebSocket, 0.5초 간격 + 이벤트 증분)
  GET  /api/state   현재 상태 JSON
  GET  /api/chart?symbol=…   선택한 종목의 차트 자료
  POST /api/control {"action": "halt" | "flatten" | "resume"}
  POST /api/ticket  {"id", "action": "fill" | "skip" | "sold", "qty", "price"}   (주문 보조 모드)

제어 API와 상태는 DASHBOARD_TOKEN 이 설정돼 있으면 토큰이 있어야 접근된다.
토큰 없이 외부 주소(0.0.0.0 등)에 바인딩하는 것은 거부한다 — 누구나 매매를 멈추거나 풀 수 있게 되기 때문이다.

브라우저를 통한 공격을 막는 장치:
  · 출처 검사 — 다른 사이트의 화면이 사용자의 브라우저로 이 서버에 명령을 보내는 것(CSRF, 웹소켓 가로채기) 차단
  · 호스트 이름 검사 — 내 컴퓨터에서만 여는 경우(127.0.0.1) localhost 계열 이름으로 온 요청만 받는다.
    공격자가 자기 도메인을 127.0.0.1 로 바꿔치기해(DNS 리바인딩) '같은 출처'인 척하는 것을 막는다.
  · 명령 본문은 JSON 만 — 단순 폼 전송으로 위장한 요청 차단
"""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path

from aiohttp import WSMsgType, web

try:
    import orjson

    def dumps(o) -> str:
        return orjson.dumps(o, option=orjson.OPT_NON_STR_KEYS).decode()
except ImportError:                                    # pragma: no cover
    def dumps(o) -> str:
        return json.dumps(o, ensure_ascii=False)

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Dashboard:
    def __init__(self, supervisor, host: str, port: int, token: str = "") -> None:
        if host not in LOOPBACK and not token:
            raise ValueError("외부에서 접속 가능한 주소로 대시보드를 열려면 DASHBOARD_TOKEN 을 설정해야 합니다.")
        self.sup = supervisor
        self.host, self.port, self.token = host, port, token
        self.local_only = host in LOOPBACK
        self.app = web.Application(middlewares=[self._host_guard])
        self.app.add_routes([web.get("/", self.index), web.get("/ws", self.ws),
                             web.get("/api/state", self.state), web.get("/api/chart", self.chart),
                             web.post("/api/control", self.control), web.post("/api/ticket", self.ticket)])
        self.runner: web.AppRunner | None = None
        self.clients: set[web.WebSocketResponse] = set()

    @web.middleware
    async def _host_guard(self, req: web.Request, handler):
        """내 컴퓨터에서만 여는 대시보드는 localhost 계열 이름으로 온 요청만 받는다(DNS 리바인딩 차단).
        외부에 여는 경우에는 토큰이 필수라, 이름을 속인 화면이 와도 토큰을 모르면 아무것도 못 한다."""
        if self.local_only:
            host = req.headers.get("Host", "")
            name = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
            if name.lower() not in ("127.0.0.1", "localhost", "[::1]"):
                return web.Response(status=403, text="forbidden host")
        return await handler(req)

    def _ok(self, req: web.Request) -> bool:
        if not self.token:
            return True
        got = req.headers.get("X-Token") or req.query.get("token", "")
        return hmac.compare_digest(got, self.token)

    @staticmethod
    def _same_origin(req: web.Request) -> bool:
        """다른 사이트가 사용자의 브라우저를 통해 이 서버에 명령을 보내는 것(CSRF, 웹소켓 가로채기)을 막는다.
        브라우저는 교차 출처 요청에 Origin 을 붙이므로, Origin 이 있으면 이 서버 주소와 같아야 한다."""
        origin = req.headers.get("Origin")
        if not origin:
            return True                                  # 브라우저가 아닌 클라이언트(curl 등)
        return origin.split("://", 1)[-1] == req.headers.get("Host", "")

    def _guard_post(self, req: web.Request) -> web.Response | None:
        if not self._same_origin(req):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        if not self._ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        if req.content_type != "application/json":
            return web.json_response({"error": "content-type must be application/json"}, status=415)
        return None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()

    async def stop(self) -> None:
        for ws in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    async def index(self, req: web.Request) -> web.Response:
        return web.Response(text=(STATIC / "index.html").read_text(encoding="utf-8"),
                            content_type="text/html", headers={"Cache-Control": "no-store"})

    async def state(self, req: web.Request) -> web.Response:
        if not self._ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.Response(text=dumps(self.sup.snapshot()), content_type="application/json")

    async def chart(self, req: web.Request) -> web.Response:
        if not self._ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        c = self.sup.chart(req.query.get("symbol", ""))
        if c is None:
            return web.json_response({"error": "unknown symbol"}, status=404)
        return web.Response(text=dumps(c), content_type="application/json")

    async def control(self, req: web.Request) -> web.Response:
        bad = self._guard_post(req)
        if bad is not None:
            return bad
        try:
            action = (await req.json()).get("action", "")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if action not in ("halt", "flatten", "resume"):
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response({"ok": True, "message": self.sup.control(action)})

    async def ticket(self, req: web.Request) -> web.Response:
        """주문 보조 모드: 사용자가 직접 낸 주문의 체결 결과를 알리거나(체결됨), 제안을 건너뛴다."""
        bad = self._guard_post(req)
        if bad is not None:
            return bad
        br = self.sup.broker
        if not hasattr(br, "confirm"):
            return web.json_response({"error": "manual mode only"}, status=400)
        try:
            d = await req.json()
            tid, action = str(d["id"]), d["action"]
            qty = int(d["qty"]) if d.get("qty") not in (None, "") else None
            price = float(d["price"]) if d.get("price") not in (None, "") else None
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if action == "fill":
            ok, msg = br.confirm(tid, qty, price)
        elif action == "skip":
            ok, msg = br.skip(tid)
        elif action == "sold":                           # 제안 없이 직접 판 것 — id 자리에 종목 코드
            if price is None:
                return web.json_response({"error": "bad request"}, status=400)
            ok, msg = self.sup.manual_sell(tid.upper()[:16], qty, price)
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        # 장부에 반영되지 않았으면 성공처럼 답하지 않는다 — 사람은 이미 실제로 주문했을 수 있다
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)

    async def ws(self, req: web.Request) -> web.StreamResponse:
        if not self._same_origin(req):
            return web.Response(status=403)
        if not self._ok(req):
            return web.Response(status=401)
        if len(self.clients) >= 8:                       # 화면이 너무 많이 붙어 매매 루프를 방해하지 않게
            return web.Response(status=503)
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(req)
        self.clients.add(ws)
        seq = 0
        sel = ""
        try:
            while not ws.closed:
                snap = self.sup.snapshot(seq)
                seq = snap["seq"]
                if sel:
                    snap["chart"] = self.sup.chart(sel)      # 선택한 종목의 실시간 차트
                await ws.send_str(dumps(snap))
                try:
                    msg = await ws.receive(timeout=0.5)
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    if msg.type == WSMsgType.TEXT and len(msg.data) < 200:
                        try:
                            sel = str(json.loads(msg.data).get("sel", ""))[:16]
                        except ValueError:
                            pass
                except asyncio.TimeoutError:
                    pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(ws)
        return ws
