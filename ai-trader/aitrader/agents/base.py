from __future__ import annotations


class Agent:
    """모든 에이전트의 공통 통계(대시보드 표시용). 핫패스에서는 정수 증가만 한다."""
    name = ""
    role = ""
    kind = "fast"            # fast(규칙·수치, 마이크로초) | llm(느린 경로)

    def __init__(self) -> None:
        self.calls = 0
        self.acts = 0
        self.last = ""
        self.last_ts = 0.0
        self.status = "ok"   # ok | idle | warn | halt

    def note(self, text: str, ts: float) -> None:
        self.acts += 1
        self.last = text
        self.last_ts = ts

    def info(self) -> dict:
        return {"name": self.name, "role": self.role, "kind": self.kind, "calls": self.calls,
                "acts": self.acts, "last": self.last, "last_ts": self.last_ts, "status": self.status}
