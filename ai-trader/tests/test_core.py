"""빠른 경로(지표·에이전트·엔진·회계) 검증."""
from aitrader.agents.chart import ChartAnalyst
from aitrader.agents.guards import OverboughtGuard
from aitrader.agents.learner import tag_causes
from aitrader.backtest import Backtester, bars_to_ticks, sim_fill_price
from aitrader.brokers.sim import SimMarket, kst_epoch
from aitrader.config import Config
from aitrader.engine import Engine
from aitrader.indicators import ATR, EMA, RSI, IndicatorSet, round_tick, tick_size
from aitrader.models import BUY, SELL, Bar, Features, Fill, OrderIntent, Regime, Tick, Trade
from aitrader.params import BOUNDS, ParamStore, StrategyParams, apply_changes
from datetime import date

OPEN = kst_epoch(date(2026, 1, 5))          # 월요일 09:00 KST


def test_tick_size_table():
    assert [tick_size(p) for p in (1_500, 3_000, 15_000, 30_000, 100_000, 300_000, 600_000)] == \
        [1, 5, 10, 50, 100, 500, 1_000]
    assert tick_size(30_000, etf=True) == 5 and tick_size(1_500, etf=True) == 1
    assert round_tick(30_010, up=True) == 30_050 and round_tick(30_040, up=False) == 30_000
    assert round_tick(30_012, True, etf=True) == 30_015


def test_indicators_basic():
    e = EMA(3)
    assert [round(e.update(x), 4) for x in (1, 2, 3, 4)] == [1.0, 1.5, 2.0, 3.0]
    r = RSI(14)
    for i in range(30):
        r.update(100 + i)
    assert r.value == 100.0                                  # 계속 오르면 RSI 100
    r2 = RSI(14)
    for i in range(30):
        r2.update(100 - i)
    assert r2.value < 1.0
    a = ATR(14)
    for _ in range(30):
        a.update(105, 95, 100)
    assert abs(a.value - 10) < 1e-9


def test_indicatorset_no_lookahead():
    cfg = Config().strategy
    ind = IndicatorSet(cfg)
    f = None
    for i in range(30):
        f = ind.update(Bar("X", i * 60.0, 100, 101 + i, 99, 100 + i, 1000))
    # 돌파 기준 고가(hh)는 현재 봉을 뺀 직전 봉들의 최고가여야 한다
    assert f.hh == 101 + 28 and f.ready


def _uptrend(rsi=60.0, vol=2.0) -> Features:
    return Features(close=10_500, ema_fast=10_400, ema_slow=10_200, rsi=rsi, atr=100, bb_upper=10_800,
                    bb_mid=10_300, vwap=10_300, hh=10_450, vol_ratio=vol, ready=True)


def test_chart_analyst_signal_and_filters():
    c, p = ChartAnalyst(), StrategyParams()
    s = c.evaluate("X", _uptrend(), p)
    assert s is not None and s.score >= p.entry_score_min
    down = _uptrend()
    down.ema_fast = 10_100                                     # 하락 추세면 신호 없음
    assert c.evaluate("X", down, p) is None
    weak = _uptrend(rsi=40.0)                                  # 모멘텀 부족
    assert c.evaluate("X", weak, p) is None


def test_overbought_guard():
    g, p = OverboughtGuard(), StrategyParams()
    assert g.check(_uptrend(), 1.0, 999, p) == ""
    assert g.check(_uptrend(rsi=80), 1.0, 999, p) == "RSI 과매수"
    ext = _uptrend()
    ext.close = 10_400 + 3 * 100                               # 단기평균 대비 3 ATR 이격
    assert g.check(ext, 1.0, 999, p) == "이격 과다"
    assert g.check(_uptrend(), 15.0, 999, p) == "당일 급등 추격"
    assert g.check(_uptrend(), 1.0, 3, p) == "손절 직후 재진입"
    assert g.blocks["RSI 과매수"] == 1


def test_sizer_risk_and_cost_floor():
    cfg = Config()
    e = Engine(cfg, StrategyParams(), 50_000_000)
    p = e.p
    # ATR이 충분히 크면 손절폭 = ATR×배수, 위험금액 = 자산×0.5%
    qty, sd = e.sizer.size(10_000, 200, 1.0, 50_000_000, 50_000_000, 0, 1.0, p, etf=True, tax=0.0)
    assert sd == 400 and qty == int(50_000_000 * 0.005 / 400)
    # ATR이 아주 작으면 '왕복 비용의 4배'가 최소 손절폭이 된다
    cost = e.sizer.round_trip_cost(70_000, False, cfg.fees.sell_tax)
    qty2, sd2 = e.sizer.size(70_000, 10, 1.0, 50_000_000, 50_000_000, 0, 1.0, p, etf=False, tax=cfg.fees.sell_tax)
    assert abs(sd2 - 4 * cost) < 1e-6
    # 종목당 비중 상한
    qty3, _ = e.sizer.size(10_000, 5_000, 1.0, 1_000_000, 1_000_000, 0, 1.0, p, etf=True)
    assert qty3 * 10_000 <= 1_000_000 * cfg.risk.max_weight
    # 시장 위험 배수 0 → 매수 안 함
    assert e.sizer.size(10_000, 200, 1.0, 50_000_000, 50_000_000, 0, 0.0, p, etf=True)[0] == 0


def test_engine_accounting_stock_and_etf():
    cfg = Config()
    cfg.etfs = ["E"]
    e = Engine(cfg, StrategyParams(), 10_000_000)
    for sym in ("E", "S"):
        e.on_tick(Tick(sym, OPEN + 600, 10_000, 1, 10_000, 10_010))
        e.on_fill(Fill(sym, BUY, 100, 10_000, OPEN + 600, ctx={"stop_dist": 200, "atr": 100}))
        tr = e.on_fill(Fill(sym, SELL, 100, 10_100, OPEN + 900, reason="take_profit"))
        c = cfg.fees.commission
        tax = 0.0 if sym == "E" else cfg.fees.sell_tax
        expect = 100 * 10_100 * (1 - c - tax) - 100 * 10_000 * (1 + c)
        assert abs(tr.pnl - expect) < 1e-6 and tr.exit_reason == "take_profit"
    assert not e.positions
    assert abs(e.cash - (10_000_000 + e.realized_total)) < 1e-6      # 현금 = 시작 + 실현손익(포지션 없을 때)
    assert abs(e.equity - e.cash) < 1e-6


def _engine_with_position(cfg=None, stop_dist=200.0):
    cfg = cfg or Config()
    cfg.etfs = ["E"]
    sent: list[OrderIntent] = []
    e = Engine(cfg, StrategyParams(), 10_000_000, execute=sent.append)
    e.on_tick(Tick("E", OPEN + 600, 10_000, 1, 10_000, 10_005))
    e.on_fill(Fill("E", BUY, 100, 10_000, OPEN + 600, ctx={"stop_dist": stop_dist, "atr": 100}))
    return e, sent


def test_stop_loss_on_tick_is_market_order():
    e, sent = _engine_with_position()
    e.on_tick(Tick("E", OPEN + 610, 9_900, 1, 9_900, 9_905))
    assert not sent
    e.on_tick(Tick("E", OPEN + 620, 9_800, 1, 9_800, 9_805))            # 손절가 도달
    assert len(sent) == 1 and sent[0].side == SELL and sent[0].reason == "stop_loss" and sent[0].limit_price == 0
    e.on_tick(Tick("E", OPEN + 621, 9_700, 1, 9_700, 9_705))            # 주문 진행 중엔 중복 주문 금지
    assert len(sent) == 1


def test_trailing_stop_ratchets_and_take_profit():
    e, sent = _engine_with_position()
    pos = e.positions["E"]
    e.on_tick(Tick("E", OPEN + 610, 10_250, 1, 10_250, 10_255))         # +1R 넘어서면 추적 시작
    assert pos.trail_on and pos.stop == 10_250 - 200 and pos.stop_kind == "trailing_stop"
    e.on_tick(Tick("E", OPEN + 620, 10_350, 1, 10_350, 10_355))
    assert pos.stop == 10_150
    e.on_tick(Tick("E", OPEN + 630, 10_100, 1, 10_100, 10_105))         # 추적손절 발동
    assert sent and sent[0].reason == "trailing_stop"
    e2, sent2 = _engine_with_position()
    e2.on_tick(Tick("E", OPEN + 610, 10_400, 1, 10_400, 10_405))        # 익절가(2R)
    assert sent2 and sent2[0].reason == "take_profit" and sent2[0].limit_price > 0


def test_eod_flatten():
    e, sent = _engine_with_position()
    t = OPEN + (15 * 3600 + 15 * 60) - 9 * 3600                         # 15:15
    e.on_tick(Tick("E", t, 10_050, 1, 10_050, 10_055))
    assert sent and sent[0].reason == "eod_flatten"


def test_daily_loss_limit_flattens_and_blocks_buys():
    e, sent = _engine_with_position(stop_dist=5_000)
    e.on_tick(Tick("E", OPEN + 700, 7_000, 1, 7_000, 7_005))            # 평가손 -30만원 = 자산의 -3% (한도 2%)
    assert e.risk.day_stop and e.risk.flatten_all
    e.on_tick(Tick("E", OPEN + 701, 7_000, 1, 7_000, 7_005))
    assert sent and sent[-1].reason == "risk_flatten"
    it = OrderIntent("E", BUY, 10, 7_005, "entry")
    why = e.risk.pre_trade(it, 7_000, OPEN + 702, 10 * 3600, 0, 0, e.equity, Regime())
    assert why == "일일 손실 한도"


def test_max_drawdown_halts_but_sells_still_allowed():
    cfg = Config()
    e, sent = _engine_with_position(cfg, stop_dist=9_000)
    e.risk.day_stop = True                                              # 일일 한도와 무관하게 낙폭만 검사
    e.on_tick(Tick("E", OPEN + 700, 1_000, 1, 1_000, 1_005))            # -9% 낙폭
    assert e.risk.halted and "낙폭" in e.risk.halt_reason
    sell = OrderIntent("E", SELL, 100, 0.0, "risk_flatten")
    assert e.risk.pre_trade(sell, 1_000, OPEN + 701, 10 * 3600, 1, 0, e.equity, Regime()) == ""   # 매도는 막지 않는다
    e.risk.new_day(e.equity)
    assert e.risk.halted                                                # 다음 날에도 자동 해제되지 않는다
    e.risk.resume()
    assert not e.risk.halted


def test_sell_never_blocked_by_notional_cap():
    cfg = Config()
    cfg.risk.max_order_notional = 1_000
    e = Engine(cfg, StrategyParams(), 10_000_000)
    sell = OrderIntent("E", SELL, 100, 0.0, "stop_loss")
    assert e.risk.pre_trade(sell, 10_000, OPEN, 10 * 3600, 1, 0, 1e7, Regime()) == ""
    buy = OrderIntent("E", BUY, 100, 10_000, "entry")
    assert e.risk.pre_trade(buy, 10_000, OPEN + 3600, 10 * 3600, 0, 0, 1e7, Regime()) == "주문금액 상한"


def test_regime_blocks_and_avoid():
    e = Engine(Config(), StrategyParams(), 10_000_000)
    buy = OrderIntent("E", BUY, 10, 10_000, "entry")
    args = (buy, 10_000, OPEN + 3600, 10 * 3600, 0, 0, 1e7)
    assert e.risk.pre_trade(*args, Regime(block_entries=True)) == "시장 위험회피"
    assert e.risk.pre_trade(*args, Regime(avoid=frozenset({"E"}))) == "시장분석 회피 종목"
    assert e.risk.pre_trade(*args, Regime()) == ""


def test_sim_fill_price():
    assert sim_fill_price(BUY, 0, 10_000, 10_000, 10_010, 1) == 10_020            # 시장가 매수: 매도1호가+1틱
    assert sim_fill_price(BUY, 10_010, 10_000, 10_000, 10_010, 1) == 10_010       # 지정가 상한
    assert sim_fill_price(BUY, 9_990, 10_000, 10_000, 10_010, 1) is None          # 체결 불가 지정가
    assert sim_fill_price(SELL, 0, 10_000, 10_000, 10_010, 1) == 9_990
    assert sim_fill_price(SELL, 10_050, 10_000, 10_000, 10_010, 1) is None


def test_backtest_deterministic_and_consistent():
    def run():
        cfg = Config()
        mk = SimMarket(cfg.universe, seed=3, tick_sec=10.0, etfs=frozenset(cfg.etfs))
        bt = Backtester(cfg, StrategyParams())
        for _ in range(3):
            bt.run_ticks(mk.day_ticks())
            bt.engine.flush_bars()
            mk.next_day()
        return bt
    a, b = run(), run()
    ra, rb = a.result(), b.result()
    assert ra == rb and ra["n"] > 0                                    # 같은 시드 → 같은 결과
    e = a.engine
    assert not e.positions                                             # 당일 청산 원칙
    assert abs(e.equity - (a.start + e.realized_total)) < 1.0          # 회계 항등식
    assert abs(sum(t.pnl for t in e.trades) - e.realized_total) < 1.0


def test_bars_to_ticks_roundtrip():
    bars = [Bar("X", 0.0, 100, 110, 95, 105, 1000), Bar("X", 180.0, 105, 106, 90, 92, 500)]
    ticks = list(bars_to_ticks(bars, 180.0))
    assert [t.price for t in ticks[:4]] == [100, 95, 110, 105]         # 양봉: 저가 먼저
    assert [t.price for t in ticks[4:]] == [105, 106, 90, 92]          # 음봉: 고가 먼저
    assert sum(t.volume for t in ticks[:4]) == 1000


def test_params_bounds_and_store(tmp_path):
    p = StrategyParams()
    q = apply_changes(p, {"rsi_overbought": 20, "stop_atr_mult": 99, "max_daily_loss": 1.0, "bogus": 1})
    lo, hi, step = BOUNDS["rsi_overbought"]
    assert q.rsi_overbought == p.rsi_overbought - step                 # 1회 변경폭 제한
    assert q.stop_atr_mult == p.stop_atr_mult + BOUNDS["stop_atr_mult"][2]
    assert not hasattr(q, "max_daily_loss")                            # 위험 한도는 학습 대상이 아니다
    store = ParamStore(tmp_path / "params.json")
    v2 = store.adopt(q, "테스트")
    assert v2.version == 2 and ParamStore(tmp_path / "params.json").current == v2
    back = store.rollback("나빠짐")
    assert back.version == 3 and back.rsi_overbought == p.rsi_overbought


def test_tag_causes():
    p = StrategyParams()
    t = Trade("X", 0, 1, 10, 100, 99, -1000, -0.01, -1.0, "stop_loss", 2, 1.0, 0.2, 1,
              {"rsi": 70, "ext_atr": 0.5, "trend": 0.2, "vol_ratio": 0.9, "breakout": 0.5, "tod": 9 * 3600 + 600})
    c = tag_causes(t, p)
    assert {"과열 추격", "빠른 손절(손절폭 부족)", "약한 추세 진입", "거래량 미확인", "돌파 없는 진입", "장 초반 진입"} <= set(c)
    win = Trade("X", 0, 1, 10, 100, 110, 1000, 0.1, 1.0, "take_profit", 5, 0.1, 2.0, 1, {})
    assert tag_causes(win, p) == []
