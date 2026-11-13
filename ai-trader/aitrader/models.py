"""핵심 데이터 모델. 핫패스에서 쓰이므로 전부 slots dataclass(속성 접근·메모리 최소화)."""
from __future__ import annotations

from dataclasses import dataclass, field

BUY = "BUY"
SELL = "SELL"


@dataclass(slots=True)
class Tick:
    symbol: str
    ts: float          # 거래소 체결 시각(epoch 초) — 엔진의 "현재 시각"은 항상 이 값
    price: float
    volume: int        # 이 틱의 체결량
    bid: float = 0.0
    ask: float = 0.0


@dataclass(slots=True)
class Bar:
    symbol: str
    ts: float          # 봉 시작 시각
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(slots=True)
class Features:
    """봉 마감 시점의 지표 스냅샷 — 차트분석 에이전트의 입력."""
    close: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi: float = 50.0
    atr: float = 0.0
    bb_upper: float = 0.0
    bb_mid: float = 0.0
    vwap: float = 0.0
    hh: float = 0.0          # 직전 N봉 고가(현재 봉 제외)
    vol_ratio: float = 1.0   # 현재 봉 거래량 / 평균 거래량
    ready: bool = False


@dataclass(slots=True)
class Signal:
    symbol: str
    score: float
    parts: dict


@dataclass(slots=True)
class OrderIntent:
    symbol: str
    side: str
    qty: int
    limit_price: float       # 0 이면 시장가
    reason: str
    ctx: dict = field(default_factory=dict)   # 진입 맥락(복기용): 지표·점수·손절폭 등


@dataclass(slots=True)
class Order:
    id: int
    symbol: str
    side: str
    qty: int
    limit_price: float
    reason: str
    ctx: dict
    created_ts: float
    status: str = "NEW"      # NEW → SENT → ACK → FILLED | CANCELED | REJECTED
    broker_id: str = ""
    broker_org: str = ""     # KIS 정정/취소에 필요한 주문조직번호
    filled: int = 0
    avg_price: float = 0.0
    sent_ns: int = 0
    rtt_ms: float = 0.0
    error: str = ""


@dataclass(slots=True)
class Fill:
    symbol: str
    side: str
    qty: int
    price: float
    ts: float
    order_id: int = 0
    reason: str = ""
    ctx: dict = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    symbol: str
    qty: int
    avg_price: float
    cost: float              # 매수 수수료 포함 총 취득원가
    entry_ts: float
    stop: float
    target: float
    risk_per_share: float    # 최초 손절폭(1R)
    trail_trigger: float     # 이 가격 이상이면 추적손절 시작
    trail_dist: float
    high: float
    low: float
    stop_kind: str = "stop_loss"
    trail_on: bool = False
    bars_held: int = 0
    realized: float = 0.0    # 부분 청산 누적 실현손익
    exit_notional: float = 0.0
    exit_qty: int = 0
    ctx: dict = field(default_factory=dict)


@dataclass(slots=True)
class Trade:
    """왕복 거래 1건 — 복기(학습) 에이전트의 분석 단위."""
    symbol: str
    entry_ts: float
    exit_ts: float
    qty: int
    entry_price: float
    exit_price: float
    pnl: float               # 수수료·세금 차감 후
    pnl_pct: float
    r_multiple: float        # 손익 / 최초 위험(1R)
    exit_reason: str
    bars_held: int
    mae_r: float             # 보유 중 최대 역행폭(R)
    mfe_r: float             # 보유 중 최대 순행폭(R)
    params_version: int
    ctx: dict = field(default_factory=dict)
    causes: list = field(default_factory=list)   # 손실 원인 태그(복기 결과)


@dataclass(slots=True)
class Regime:
    """시장분석 에이전트의 출력. 핫패스는 이 객체를 잠금 없이 읽기만 한다."""
    bias: str = "neutral"        # risk_on | neutral | risk_off
    risk_mult: float = 1.0       # 0~1, 포지션 크기 배수
    block_entries: bool = False
    note: str = ""
    source: str = "rule"         # rule | llm
    ts: float = 0.0
    avoid: frozenset = frozenset()


@dataclass(slots=True)
class AccountSnapshot:
    cash: float
    positions: dict              # symbol -> (qty, avg_price)
