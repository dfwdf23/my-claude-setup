"""알림 — 사람이 없을 때 중요한 일(정지, 체결 제안, 심각한 악재, 장 마감 요약)을 휴대폰으로 보낸다.

텔레그램 봇만 지원한다(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). 설정이 없으면 아무것도 하지 않는다.
알림 실패는 매매에 영향을 주지 않는다.
"""
from __future__ import annotations

import asyncio
import time

from .config import NotifyCfg


class Notifier:
    def __init__(self, cfg: NotifyCfg, base_url: str = "https://api.telegram.org") -> None:
        self.token = cfg.telegram_token
        self.chat = cfg.telegram_chat
        self.base = base_url
        self.enabled = bool(self.token and self.chat)
        self.sent = 0
        self.failed = 0
        self._last = 0.0
        self._q: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def send(self, text: str) -> None:
        """논블로킹. 이벤트 루프 안에서 호출해야 한다."""
        if not self.enabled:
            return
        if self._q is None:
            self._q = asyncio.Queue(maxsize=50)
            self._task = asyncio.get_running_loop().create_task(self._run())
        try:
            self._q.put_nowait(text[:3500])
        except asyncio.QueueFull:
            self.failed += 1

    async def _run(self) -> None:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            while True:
                text = await self._q.get()
                wait = 1.0 - (time.monotonic() - self._last)      # 초당 1건 이하
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                try:
                    async with s.post(f"{self.base}/bot{self.token}/sendMessage",
                                      json={"chat_id": self.chat, "text": text}) as r:
                        if r.status == 200:
                            self.sent += 1
                        else:
                            self.failed += 1
                except Exception:
                    self.failed += 1
                finally:
                    self._q.task_done()

    async def close(self, grace: float = 6.0) -> None:
        """종료. 아직 못 보낸 알림(예: '오류로 멈춥니다')은 잠깐 기다려 보내고 끝낸다."""
        if self._task is not None:
            try:
                await asyncio.wait_for(self._q.join(), grace)
            except (asyncio.TimeoutError, Exception):
                pass
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
