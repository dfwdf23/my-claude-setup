"""Claude 호출 계층 — 토큰 절약 장치가 모두 여기 모여 있다.

1. 핫패스(틱·주문)에서는 절대 호출하지 않는다. 호출은 '시장분석'과 '장후 복기' 두 곳뿐.
2. 같은 입력이면 다시 묻지 않는다(입력 해시 캐시).
3. 하루 토큰 예산을 넘으면 호출을 끊고 규칙 기반으로만 동작한다.
4. 입력은 원시 시세가 아니라 압축한 수치 요약, 출력은 JSON 스키마로 강제(잡담 토큰 없음).
5. 시스템 프롬프트는 고정 문자열 + 캐시 표시(연속 호출 시 입력 토큰 할인).
6. API 키가 없거나 호출이 실패해도 매매는 멈추지 않는다 — None 을 돌려주고 규칙 기반으로 대체.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

from .config import LLMCfg

log = logging.getLogger("aitrader.llm")

# 100만 토큰당 달러(입력, 출력) — 비용 추정 표시용. 모델을 바꾸면 여기에 추가.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}
# 거절 시 서버 측 대체 모델 자동 전환(해당 모델만 지원)
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}


class LLM:
    def __init__(self, cfg: LLMCfg, journal=None) -> None:
        self.cfg = cfg
        self.journal = journal
        self.client = None
        self.enabled = False
        self.status = "꺼짐"
        self.cache: dict[str, dict] = {}
        self.day = ""
        self.tokens_today = 0
        self.calls_today = 0
        self.cache_hits = 0
        self.skipped_budget = 0
        self.cost_today = 0.0
        self.cost_total = 0.0
        self.tokens_total = 0
        self.last_error = ""
        if not cfg.enabled:
            return
        try:
            import anthropic
            self._anthropic = anthropic
            # 인증은 SDK가 환경에서 찾는다(ANTHROPIC_API_KEY 등). 키를 코드에 넣지 않는다.
            self.client = anthropic.AsyncAnthropic(timeout=cfg.timeout_sec, max_retries=2)
            self.enabled = True
            self.status = "대기"
        except Exception as e:                        # SDK 미설치·인증정보 없음
            self.status = "사용 불가(규칙 기반으로 동작)"
            self.last_error = repr(e)

    def new_day(self, day: str) -> None:
        """day: 'YYYY-MM-DD'(이 컴퓨터의 날짜). 날짜가 바뀌면 하루 예산을 새로 시작한다.
        프로그램을 다시 켠 경우에는 일지에 남은 오늘 사용량을 이어받는다 — 그러지 않으면 재시작할 때마다
        예산이 0부터 다시 시작돼, 반복 재시작(오류로 죽고 다시 뜨는 경우 등)에서 하루 한도가 무의미해진다."""
        if day != self.day:
            first = self.day == ""
            self.day = day
            self.tokens_today = 0
            self.calls_today = 0
            self.cost_today = 0.0
            self.cache.clear()
            if first and self.journal is not None:
                try:
                    t0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
                    self.tokens_today, self.calls_today, self.cost_today = self.journal.llm_since(t0)
                except Exception as e:                # 사용량을 못 읽어도 매매에는 지장 없다
                    log.debug("오늘 사용량 복원 실패: %r", e)

    def budget_left(self) -> int:
        return max(0, self.cfg.daily_token_budget - self.tokens_today)

    async def ask_json(self, role: str, model: str, effort: str, system: str, payload: dict,
                       schema: dict, max_tokens: int = 4096) -> dict | None:
        """구조화(JSON) 응답 한 번. 실패·예산초과·비활성 시 None."""
        if not self.enabled:
            return None
        user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        key = hashlib.sha1(f"{role}|{model}|{user}".encode()).hexdigest()
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        if self.tokens_today >= self.cfg.daily_token_budget:
            self.skipped_budget += 1
            self.status = "일일 토큰 예산 소진(규칙 기반으로 동작)"
            return None
        a = self._anthropic
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if model in _FALLBACK_MODELS:
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = await self.client.messages.create(**kwargs)
        except a.AuthenticationError as e:
            self.enabled = False
            self.status = "인증 실패 — API 키 확인 필요(규칙 기반으로 동작)"
            self.last_error = str(e)
            return None
        except a.RateLimitError as e:
            self.status = "요청 한도 초과(잠시 후 재시도)"
            self.last_error = str(e)
            return None
        except a.APIStatusError as e:
            self.status = f"API 오류 {e.status_code}"
            self.last_error = str(e)
            log.warning("LLM %s 실패: %s", role, e)
            return None
        except a.APIConnectionError as e:
            self.status = "네트워크 오류"
            self.last_error = str(e)
            return None
        except Exception as e:                        # 인증정보 없음 등 SDK 내부 오류
            self.enabled = False
            self.status = "사용 불가(규칙 기반으로 동작)"
            self.last_error = repr(e)
            return None

        self._account(role, resp)
        if resp.stop_reason in ("refusal", "max_tokens"):
            self.status = f"응답 불완전({resp.stop_reason})"
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.status = "JSON 해석 실패"
            return None
        self.cache[key] = data
        self.status = "정상"
        return data

    def _account(self, role: str, resp) -> None:
        u = resp.usage
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        model = getattr(resp, "model", "") or ""
        pi, po = PRICES.get(model, PRICES.get(next((m for m in PRICES if model.startswith(m)), ""), (4.0, 20.0)))
        cost = (inp * pi + cr * pi * 0.1 + cw * pi * 2.0 + out * po) / 1e6
        total = inp + out + cr + cw
        self.tokens_today += total
        self.tokens_total += total
        self.calls_today += 1
        self.cost_today += cost
        self.cost_total += cost
        if self.journal is not None:
            self.journal.llm_usage(time.time(), role, model, inp, out, cr, cw, cost)

    def info(self) -> dict:
        return {"enabled": self.enabled, "status": self.status, "tokens_today": self.tokens_today,
                "budget": self.cfg.daily_token_budget, "calls_today": self.calls_today,
                "cache_hits": self.cache_hits, "skipped_budget": self.skipped_budget,
                "cost_today": round(self.cost_today, 4), "cost_total": round(self.cost_total, 4),
                "model_market": self.cfg.model_market, "model_review": self.cfg.model_review}
