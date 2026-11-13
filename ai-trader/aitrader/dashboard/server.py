"""실시간 총괄 대시보드 서버 (aiohttp).

  GET  /            대시보드 화면
  GET  /ws          실시간 상태 푸시(WebSocket, 0.5초 간격 + 이벤트 증분)
  GET  /api/state   현재 상태 JSON
  POST /api/control {"action": "halt" | "flatten" | "resume"}

제어 API와 상태는 DASHBOARD_TOKEN 이 설정돼 있으면 토큰이 있어야 접근된다.
토큰 없이 외부 주소(0.0.0.0 등)에 바인딩하는 것은 거부한다 — 누구나 매매를 멈추거나 풀 수 있게 되기 때문이다.
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
        self.app = web.Application()
        self.app.add_routes([web.get("/", self.index), web.get("/ws", self.ws),
                             web.get("/api/state", self.state), web.post("/api/control", self.control)])
        self.runner: web.AppRunner | None = None
        self.clients: set[web.WebSocketResponse] = set()

    def _ok(self, req: web.Request) -> bool:
        if not self.token:
            return True
        got = req.headers.get("X-Token") or req.query.get("token", "")
        return hmac.compare_digest(got, self.token)

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

    async def control(self, req: web.Request) -> web.Response:
        if not self._ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            action = (await req.json()).get("action", "")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if action not in ("halt", "flatten", "resume"):
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response({"ok": True, "message": self.sup.control(action)})

    async def ws(self, req: web.Request) -> web.StreamResponse:
        if not self._ok(req):
            return web.Response(status=401)
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(req)
        self.clients.add(ws)
        seq = 0
        try:
            while not ws.closed:
                snap = self.sup.snapshot(seq)
                seq = snap["seq"]
                await ws.send_str(dumps(snap))
                try:
                    msg = await ws.receive(timeout=0.5)
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                except asyncio.TimeoutError:
                    pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(ws)
        return ws
