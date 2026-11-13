"""설정. config.toml(없으면 기본값) + 환경변수(비밀값).

시장(market)에 따라 기본값이 달라진다: Config() 는 국내(KRX), Config.for_market("US") 는 미국.
명령줄(load_config)은 config.toml 에 market 이 없으면 미국(US)을 기본으로 쓴다.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

from .market import Market, get_market, hhmm


@dataclass
class SessionCfg:
    """모든 시각은 '거래소 현지 시각'(KRX=한국, US=뉴욕)."""
    open: str = "09:00"
    close: str = "15:30"
    entry_start: str = "09:05"     # 시초가 변동성 구간은 건너뜀
    entry_end: str = "14:50"       # 이후 신규 매수 금지
    flatten: str = "15:15"         # intraday_only 일 때 전량 청산 시각
    intraday_only: bool = True     # 당일 청산(오버나이트 위험 제거)
    pre_open: str = "08:40"        # 장전 준비(종목선정·시장분석)
    post: str = "15:40"            # 장후 복기

    def tod(self, name: str) -> int:
        return hhmm(getattr(self, name))


@dataclass
class RiskCfg:
    risk_per_trade: float = 0.005      # 거래당 위험 = 자산의 0.5%
    max_positions: int = 5
    max_weight: float = 0.20           # 종목당 최대 비중
    max_exposure: float = 0.80         # 총 주식 비중 상한
    cash_buffer: float = 0.02
    max_daily_loss: float = 0.02       # 당일 -2% → 신규매수 중단 + 전량청산
    max_drawdown: float = 0.08         # 고점 대비 -8% → 완전 정지(수동 해제 필요)
    max_orders_per_min: int = 20
    max_trades_per_day: int = 40
    max_consec_losses: int = 4         # 연속 손실 시 냉각
    cooldown_min: int = 20
    max_order_notional: float = 20_000_000
    price_band: float = 0.05           # 주문가가 현재가 ±5% 밖이면 거부
    stale_feed_sec: float = 15.0       # 시세 끊김 감지
    max_rejects_per_min: int = 5
    order_timeout_sec: float = 10.0
    min_stop_cost_mult: float = 4.0    # 손절폭(1R)은 왕복 거래비용의 최소 4배 — 비용에 먹히는 거래 방지


@dataclass
class FeeCfg:
    commission: float = 0.00015        # 매수·매도 각각(증권사·계좌별로 다름 — 본인 요율로 수정)
    sell_tax: float = 0.0020           # 매도 시 세금·제비용(국내 주식 거래세 / 미국 SEC·TAF 수수료)
    slippage_ticks: int = 1


@dataclass
class StrategyCfg:
    bar_sec: int = 180                 # 3분봉. 1분봉은 손절폭이 거래비용에 묻혀 구조적으로 불리하다
    ema_fast: int = 9
    ema_slow: int = 21
    rsi: int = 14
    atr: int = 14
    bb: int = 20
    bb_k: float = 2.0
    breakout: int = 20
    vol_avg: int = 20
    warmup_bars: int = 25
    view_sec: int = 300                # 화면용 분봉 길이(전략 봉이 이보다 길 때만 따로 만든다)


@dataclass
class LLMCfg:
    enabled: bool = True
    model_market: str = "claude-opus-5-5"
    model_review: str = "claude-opus-5-5"
    model_news: str = "claude-opus-5-5"
    effort_market: str = "low"
    effort_review: str = "medium"
    effort_news: str = "low"
    daily_token_budget: int = 150_000  # 하루 토큰 상한 — 넘으면 규칙 기반으로만 동작
    market_min_interval_min: int = 30  # 장중 시장분석 최소 호출 간격
    market_max_calls: int = 10
    news_min_interval_min: int = 5     # 뉴스 일괄 분류 최소 간격
    news_max_calls: int = 60
    timeout_sec: float = 60.0


@dataclass
class LearnCfg:
    enabled: bool = True
    lookback_days: int = 10            # 검증 재생에 쓰는 최근 거래일 수
    holdout_frac: float = 0.35         # 그중 가장 최근 구간은 '후보를 만들 때 보지 않은' 표본외 검증용
    min_trades: int = 8                # 이보다 표본이 적으면 파라미터를 건드리지 않음
    min_improve: float = 0.05          # 후보가 기준 대비 이만큼 좋아야 채택
    rollback_min_trades: int = 20
    rollback_min_days: int = 2         # 새 버전을 최소 이틀은 지켜본 뒤 롤백 여부 판단
    min_days_between_changes: int = 3  # 파라미터를 매일 바꾸면 효과를 측정할 수 없다


@dataclass
class NewsCfg:
    enabled: bool = True
    poll_sec: float = 90.0             # 종목 전체를 한 바퀴 도는 주기
    feeds: list = field(default_factory=list)   # RSS/Atom 주소. {symbol} 이 있으면 종목별로 호출
    max_age_min: float = 90.0          # 이보다 오래된 소식은 매매 판단에 쓰지 않는다(화면에는 표시)
    block_min: float = 30.0            # 악재 감지 후 해당 종목 신규 매수 차단 시간
    half_life_min: float = 60.0        # 소식 점수가 절반으로 줄어드는 시간
    exit_on_severe: bool = True        # Claude가 '심각한 악재'로 확인하면 보유분 청산
    aliases: dict = field(default_factory=dict)  # 종목 → 회사명 목록(일반 뉴스에서 종목 찾기)
    user_agent: str = "Mozilla/5.0 (compatible; aitrader-news)"


@dataclass
class DashCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""                    # 제어 API(정지/재개/청산) 보호용. 외부 공개 시 필수


@dataclass
class KISCfg:
    env: str = "mock"                  # mock(모의투자) | real(실전)
    app_key: str = ""
    app_secret: str = ""
    account: str = ""                  # 계좌번호 앞 8자리
    product: str = "01"
    hts_id: str = ""                   # 체결통보 수신용
    exchange: str = "KRX"              # 국내 주문 거래소 구분
    exchanges: dict = field(default_factory=dict)   # 해외: 종목 → 거래소(NAS/NYS/AMS). 없으면 자동 탐색


@dataclass
class SimCfg:
    speed: float = 120.0               # 시뮬레이션 배속(1 = 실시간)
    tick_sec: float = 5.0
    seed: int = 7
    days: int = 0                      # 0 = 무한
    latency_ms: float = 2.0            # 모의 주문 왕복 지연(시뮬레이션 시간 기준)


@dataclass
class ManualCfg:
    """주문 보조 모드: 봇이 주문 내용을 만들고 사람이 증권사 앱에서 직접 체결한다."""
    feed: str = "yahoo"                # 시세 출처: yahoo(비공식·무료, 폴링) | kis
    poll_sec: float = 20.0
    ticket_ttl_min: float = 5.0        # 이 시간 안에 체결 입력이 없으면 주문 제안을 만료


@dataclass
class NotifyCfg:
    telegram_token: str = ""
    telegram_chat: str = ""


@dataclass
class Config:
    mode: str = "sim"                  # sim | kis-mock | kis-live | manual
    market: str = "KRX"                # KRX | US
    style: str = "intraday"            # swing(일봉, 며칠~몇 주 보유) | intraday(분봉, 당일 청산)
    params: dict = field(default_factory=dict)   # 전략 파라미터 기본값 덮어쓰기(학습 시작점)
    data_dir: str = "data"
    capital: float = 50_000_000        # sim 시작 자본 / 실매매: 봇이 쓸 수 있는 최대 금액
    # 국내 기본 종목: 유동성 큰 ETF 위주. ETF는 매도 시 거래세가 없고 호가단위(5원)가 작아
    # 왕복 비용이 일반 주식의 1/4 수준이다 — 잦은 자동매매는 비용이 수익을 좌우한다.
    universe: list = field(default_factory=lambda: [
        "069500", "122630", "114800", "252670", "229200", "233740", "251340", "005930"])
    etfs: list = field(default_factory=lambda: [
        "069500", "122630", "114800", "252670", "229200", "233740", "251340"])
    inverse: list = field(default_factory=lambda: ["114800", "252670", "251340"])   # 시장 방향 계산 시 뒤집는 종목
    max_symbols: int = 8
    session: SessionCfg = field(default_factory=SessionCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    fees: FeeCfg = field(default_factory=FeeCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    learn: LearnCfg = field(default_factory=LearnCfg)
    news: NewsCfg = field(default_factory=NewsCfg)
    dashboard: DashCfg = field(default_factory=DashCfg)
    kis: KISCfg = field(default_factory=KISCfg)
    sim: SimCfg = field(default_factory=SimCfg)
    manual: ManualCfg = field(default_factory=ManualCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)

    @property
    def mkt(self) -> Market:
        return get_market(self.market)

    @classmethod
    def for_market(cls, code: str, style: str | None = None) -> "Config":
        """시장·매매 방식별 기본 설정.

        미국 기본은 swing 이다. 실제 과거 시세 검증(README '실제 데이터 검증')에서 분봉 단타는 수수료를
        넣으면 손실이었고, 일봉 기준으로 몇 주 보유하는 방식만 수수료 후에도 이익이었기 때문이다.
        """
        code = code.upper()
        get_market(code)
        if code == "KRX":
            if style not in (None, "intraday"):
                raise ValueError("국내(KRX)는 현재 intraday 방식만 기본값이 준비돼 있습니다")
            return cls()
        style = style or "swing"
        if style not in ("swing", "intraday"):
            raise ValueError(f"알 수 없는 매매 방식: {style} (swing 또는 intraday)")
        cfg = cls(market="US", capital=30_000.0, style=style)
        cfg.risk.max_order_notional = 15_000.0
        if style == "swing":
            # 인버스 ETF는 오래 들고 있으면 구조적으로 가치가 깎이므로 며칠~몇 주 보유하는 방식에서는 뺀다
            cfg.universe = ["SPY", "QQQ", "TQQQ", "SOXL", "NVDA", "TSLA", "AAPL", "MSFT", "AMD", "META"]
            cfg.etfs = ["SPY", "QQQ", "TQQQ", "SOXL"]
            cfg.inverse = []
            cfg.max_symbols = 10
            cfg.strategy.bar_sec = 86_400          # 일봉: 전날 봉이 닫힌 뒤 다음 날 장 초반에 판단
            cfg.session = SessionCfg(open="09:30", close="16:00", entry_start="09:35", entry_end="15:55",
                                     flatten="15:50", intraday_only=False, pre_open="09:00", post="16:10")
            cfg.risk.max_positions = 10
            cfg.risk.max_weight = 0.10
            cfg.risk.max_exposure = 0.95
            cfg.risk.max_trades_per_day = 20
            # 수익은 길게, 손절은 넉넉히: 익절 5R, 1.5R부터 추적손절(3 ATR), 손절 2.5 ATR
            cfg.params = {"take_profit_r": 5.0, "trail_start_r": 1.5, "trail_atr_mult": 3.0,
                          "stop_atr_mult": 2.5, "time_stop_bars": 60, "cooldown_bars": 5,
                          "rsi_overbought": 85.0, "max_ext_atr": 4.0, "max_day_gain_pct": 25.0}
            cfg.learn.lookback_days = 250          # 일봉이라 1년치 기록으로 검증
            cfg.learn.min_days_between_changes = 20
            cfg.learn.rollback_min_trades = 10
            cfg.learn.rollback_min_days = 20
        else:
            # 분봉 단타(당일 청산). 실제 데이터 검증에서는 손실이었다 — 연구·실험용.
            cfg.universe = ["SPY", "QQQ", "TQQQ", "SQQQ", "SOXL", "SOXS", "NVDA", "TSLA", "AAPL", "MSFT", "AMD", "META"]
            cfg.etfs = ["SPY", "QQQ", "TQQQ", "SQQQ", "SOXL", "SOXS"]
            cfg.inverse = ["SQQQ", "SOXS"]
            cfg.max_symbols = 12
            cfg.session = SessionCfg(open="09:30", close="16:00", entry_start="09:40", entry_end="15:30",
                                     flatten="15:50", intraday_only=True, pre_open="09:00", post="16:10")
        # 국내 증권사의 미국주식 온라인 수수료는 표준 0.25%(이벤트 적용 시 0.07~0.1%) — 본인 요율로 반드시 수정.
        cfg.fees = FeeCfg(commission=0.0025, sell_tax=0.00003, slippage_ticks=2)
        cfg.kis.exchange = "NASD"
        cfg.kis.exchanges = {"SPY": "AMS", "QQQ": "NAS", "TQQQ": "NAS", "SQQQ": "NAS", "SOXL": "AMS",
                             "SOXS": "AMS", "NVDA": "NAS", "TSLA": "NAS", "AAPL": "NAS", "MSFT": "NAS",
                             "AMD": "NAS", "META": "NAS"}
        # 종목별 뉴스 피드(개인적·비상업적 이용 조건의 공개 RSS). 피드 약관 준수는 사용자 책임 — 원하면 바꾸거나 비울 것.
        cfg.news.feeds = [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
            "https://www.nasdaq.com/feed/rssoutbound?symbol={symbol}",
        ]
        cfg.news.aliases = {"NVDA": ["Nvidia"], "TSLA": ["Tesla"], "AAPL": ["Apple"], "MSFT": ["Microsoft"],
                            "AMD": ["Advanced Micro Devices"], "META": ["Meta Platforms"]}
        return cfg


def _merge(obj, data: dict) -> None:
    names = {f.name for f in fields(obj)}
    for k, v in data.items():
        if k not in names:
            raise ValueError(f"알 수 없는 설정 키: {k}")
        cur = getattr(obj, k)
        if is_dataclass(cur) and isinstance(v, dict):
            _merge(cur, v)
        elif isinstance(cur, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k} 는 true/false 여야 합니다")
            setattr(obj, k, v)
        elif isinstance(cur, (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool):
            setattr(obj, k, type(cur)(v))
        elif type(v) is type(cur) or cur is None:
            setattr(obj, k, v)
        else:
            raise ValueError(f"{k} 의 값 형식이 맞지 않습니다: {type(v).__name__} (필요: {type(cur).__name__})")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")     # 값 뒤의 주석 제거
        if v:
            os.environ.setdefault(k.strip(), v)


def load_config(path: str | None = None, market: str | None = None, style: str | None = None) -> Config:
    load_dotenv()
    p = Path(path or "config.toml")
    data: dict = {}
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    elif path:
        raise FileNotFoundError(path)
    cfg = Config.for_market(market or data.get("market", "US"), style or data.get("style"))
    data.pop("market", None)
    data.pop("style", None)
    _merge(cfg, data)
    env = os.environ
    k = cfg.kis
    k.app_key = env.get("KIS_APP_KEY", k.app_key)
    k.app_secret = env.get("KIS_APP_SECRET", k.app_secret)
    k.account = env.get("KIS_ACCOUNT", k.account)
    k.product = env.get("KIS_PRODUCT", k.product)
    k.hts_id = env.get("KIS_HTS_ID", k.hts_id)
    cfg.dashboard.token = env.get("DASHBOARD_TOKEN", cfg.dashboard.token)
    cfg.notify.telegram_token = env.get("TELEGRAM_BOT_TOKEN", cfg.notify.telegram_token)
    cfg.notify.telegram_chat = env.get("TELEGRAM_CHAT_ID", cfg.notify.telegram_chat)
    return cfg
