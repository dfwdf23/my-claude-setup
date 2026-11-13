"""학습 가능한 전략 파라미터 + 버전 관리.

복기 에이전트는 여기 정의된 범위(BOUNDS) 안에서, 한 번에 MAX_STEP 이하로만 값을 바꿀 수 있다.
위험 한도(일일 손실 한도 등)는 여기 없다 — 학습이 위험 한도를 풀 수 없게 하기 위해서다.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class StrategyParams:
    version: int = 1
    entry_score_min: float = 0.62   # 매수 신호 최소 점수
    rsi_min: float = 45.0           # 이보다 약하면 모멘텀 부족으로 보고 매수 안 함
    rsi_overbought: float = 72.0    # 과매수 방지: 이 이상이면 매수 차단
    max_ext_atr: float = 2.5        # 과매수 방지: (종가-단기EMA)/ATR 상한 — 이격 과다
    max_day_gain_pct: float = 12.0  # 과매수 방지: 당일 급등 종목 추격 금지
    vol_ratio_min: float = 1.2      # 거래량 확인 기준(봉 거래량 / 평균)
    rvol_min: float = 0.7           # 상대 거래량 하한(평소 이 시각 대비 오늘 누적 거래량)
    stop_atr_mult: float = 2.0      # 손절폭 = ATR × 배수
    take_profit_r: float = 2.0      # 익절 = 손절폭의 몇 배(R)
    trail_start_r: float = 1.0      # 이만큼 수익 나면 추적손절 시작
    trail_atr_mult: float = 2.0     # 추적손절 간격
    time_stop_bars: int = 45        # 이 봉 수 동안 수익이 안 나면 정리
    cooldown_bars: int = 10         # 손절 후 같은 종목 재진입 금지 봉 수
    size_mult: float = 1.0          # 성과 기반 크기 조절(부진하면 축소)


# (최소, 최대, 1회 최대 변경폭)
BOUNDS: dict[str, tuple[float, float, float]] = {
    "entry_score_min": (0.45, 0.90, 0.04),
    "rsi_min": (35.0, 60.0, 3.0),
    "rsi_overbought": (60.0, 85.0, 3.0),
    "max_ext_atr": (1.0, 4.0, 0.3),
    "max_day_gain_pct": (5.0, 25.0, 2.0),
    "vol_ratio_min": (0.8, 3.0, 0.2),
    "rvol_min": (0.3, 3.0, 0.2),
    "stop_atr_mult": (1.0, 4.0, 0.3),
    "take_profit_r": (1.0, 5.0, 0.4),
    "trail_start_r": (0.5, 3.0, 0.3),
    "trail_atr_mult": (1.0, 4.0, 0.3),
    "time_stop_bars": (10, 240, 10),
    "cooldown_bars": (0, 60, 5),
    "size_mult": (0.25, 1.0, 0.25),
}
_INT = {"time_stop_bars", "cooldown_bars"}


def apply_changes(base: StrategyParams, changes: dict) -> StrategyParams:
    """변경안을 범위·보폭 제한에 맞춰 적용한 새 파라미터를 돌려준다(버전은 그대로)."""
    d = asdict(base)
    for k, v in changes.items():
        if k not in BOUNDS:
            continue                      # 모르는 키·위험 한도 등은 무시
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        lo, hi, step = BOUNDS[k]
        cur = float(d[k])
        v = max(cur - step, min(cur + step, v))
        v = max(lo, min(hi, v))
        d[k] = int(round(v)) if k in _INT else round(v, 4)
    return StrategyParams(**d)


def diff(a: StrategyParams, b: StrategyParams) -> dict:
    return {f.name: (getattr(a, f.name), getattr(b, f.name))
            for f in fields(a) if f.name != "version" and getattr(a, f.name) != getattr(b, f.name)}


def _load(d: dict) -> StrategyParams:
    """저장된 값에서 지금 버전이 아는 항목만 읽는다(버전이 달라도 깨지지 않게)."""
    known = {f.name for f in fields(StrategyParams)}
    return StrategyParams(**{k: v for k, v in d.items() if k in known})


class ParamStore:
    """params.json 에 현재값과 변경 이력을 보관. 롤백 가능."""

    def __init__(self, path: str | Path | None, defaults: dict | None = None):
        """defaults: 매매 방식별 시작값(저장된 파일이 없을 때만 쓰인다)."""
        self.path = Path(path) if path else None
        self.current = _load({**asdict(StrategyParams()), **(defaults or {})})
        self.history: list[dict] = []
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.current = _load(data["current"])
            self.history = data.get("history", [])
        if not self.history:
            self.history.append({"version": self.current.version, "params": asdict(self.current),
                                 "reason": "초기값", "ts": time.time()})

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"current": asdict(self.current), "history": self.history[-200:]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def adopt(self, new: StrategyParams, reason: str, metrics: dict | None = None) -> StrategyParams:
        d = asdict(new)
        d["version"] = self.current.version + 1
        self.current = StrategyParams(**d)
        self.history.append({"version": self.current.version, "params": d, "reason": reason,
                             "ts": time.time(), "metrics": metrics or {}})
        self.save()
        return self.current

    def tweak(self, name: str, value: float) -> None:
        """버전을 올리지 않는 미세 조정(성과 기반 크기 배수용)."""
        setattr(self.current, name, value)
        self.history[-1]["params"][name] = value
        self.save()

    def previous(self) -> StrategyParams | None:
        if len(self.history) < 2:
            return None
        return _load(self.history[-2]["params"])

    def rollback(self, reason: str) -> StrategyParams | None:
        prev = self.previous()
        if prev is None:
            return None
        return self.adopt(prev, f"롤백: {reason}")
