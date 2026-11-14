# ai-trader — 작업 안내 (Claude용, 짧게 유지)

AI 자동매매 시스템. 기본은 **미국 주식 · 스윙(일봉)**. 상세는 README.md. 여기에는 다음 세션이 코드를 다시 다 읽지 않도록 핵심만 적는다.

## 명령
- 테스트: `.venv/bin/python -m pytest -q` (전부 통과해야 커밋)
- 연습 실행: `.venv/bin/python -m aitrader run --speed 600` → http://127.0.0.1:8787 (`--style intraday`, `--market krx` 가능)
- 주문 보조(실제 시세, 사람이 체결): `.venv/bin/python -m aitrader run --mode manual`
- 실제 과거 시세 검증: `python -m aitrader fetch --dir hist` 후 `python -m aitrader research --dir hist`
- 지연 측정: `.venv/bin/python -m aitrader bench`

## 구조(파일 → 책임)
- `aitrader/engine.py` 빠른 경로 전체(틱→주문). **여기에 await·LLM·I/O 금지.**
- `aitrader/agents/` chart(차트분석) · guards(과매수 방지, 리스크) · trade(비중조절, 매수, 매도) · market(시장분석, 종목선정) · news(정보수집) · learner(복기)
- `aitrader/market.py` 시장 프로필(KRX/US: 시간대·서머타임·통화·호가단위·휴장일) · `config.py` 시장×방식별 기본값(`Config.for_market`)
- `aitrader/executor.py` 주문 집행 · `supervisor.py` 총괄(일과·스냅샷·대사·복원) · `llm.py` Claude 호출/예산 · `notify.py` 텔레그램
- `aitrader/brokers/` base(인터페이스) · sim(합성 시세) · kis(국내) · kis_overseas(미국) · manual(주문 보조: 주문 제안 → 사람이 체결)
- `aitrader/backtest.py` 기록 재생 · `research.py` 실제 과거 시세 검증
- `aitrader/dashboard/` server.py + static/index.html(단일 파일, 외부 CDN 없음)

## 지켜야 할 불변 조건
- 매도 주문은 어떤 한도로도 막지 않는다(위험 축소는 항상 허용).
- 학습(`params.BOUNDS`)은 전략 파라미터만 바꾼다. `RiskCfg` 한도는 학습 대상이 아니다.
- 파라미터 변경은 반드시 `Learner.validate`(설계 구간 + 보지 않은 검증 구간 재생)를 통과해야 한다. 미국 스윙은 `auto_apply=False`(제안만) — 실제 시세 검증에서 자동 적용이 수익을 낮췄다.
- 실전(`kis-live`)은 `AITRADER_LIVE_CONFIRM` 없이는 시작되지 않는다. 이 가드를 약화시키지 말 것.
- **사람의 컴퓨터 화면을 조작해 주문을 넣는 기능(HTS 매크로·원격 조종 주문)은 만들지 않는다.** 대체 수단이 주문 보조 모드다.
- Claude 호출은 `llm.LLM.ask_json` 한 곳으로만. 새 호출을 추가하면 예산·게이팅을 함께 설계한다.
- 대시보드에 표시하는 문자열은 `textContent`로만 넣는다(종목명·소식 제목 등은 외부 데이터). 제어 API는 출처 검사·토큰·JSON 본문을 모두 확인한다.
- 일봉의 시각은 '그날 장 시작'(현지)이다. 봉 시각으로 날짜를 구할 때는 `market.local()`을 쓴다.
- 체결은 '주문별 누적 수량' 기준으로 합친다(`executor._apply`/`_late`). 통보(건별 가격)와 조회(누적 평균가)가 겹치거나 늦게 와도 이중 반영·누락이 없어야 한다. 체결 처리를 고치면 `tests/test_review2.py`의 Desk 시나리오와 `tests/test_review4.py`의 주문 처리 테스트를 먼저 돌릴 것.
- `BrokerError` = 증권사의 명시적 거부(접수 안 됨이 확실). 결과를 모르는 실패는 `BrokerUnknown`·시간 초과로 올려 집행기가 조회로 확정하게 한다. 모르는 것을 '거부'나 '취소됨'으로 단정하지 않는다.
  - `poll_order` 는 주문 내역에 그 주문이 없으면 값을 지어내지 말고 `BrokerUnknown` 을 올린다.
  - 취소 확정은 증권사의 '끝남' 표시 또는 같은 체결 수량 연속 3회(`SETTLE_SAME`)로만. 취소가 명시적으로 거부되면 조회로 확정하고, 닿았는지 모르는 실패가 3회면 STUCK(잠금 + 정지).
  - 재시작 정리(`executor.recover`)는 확인하지 못한 주문을 잊지 않는다(STUCK 으로 남김). STUCK 주문은 30초마다 다시 조회해 스스로 정리된다.
  - 주문을 낸 시각은 `Order.wall_ts`(실제 시각)와 `created_ts`(집행기 시계) — 둘 다 나중에 바꾸지 않는다. 시간 초과를 다시 잴 때는 `timer_ts`. 증권사 주문 내역과 시각을 견줄 때는 `wall_ts + broker.clock_off`.
- 재시작 복원 순서(`supervisor._restore`): 저장된 장부(마지막 가격으로 평가) → 꺼져 있던 동안의 체결 → 계좌 비교. 계좌 불일치는 판 것으로 기록 + 신규 매수 정지(전량 청산 금지).
- 장전 준비 순서(`supervisor.day_start`): 일봉 맥락 → 전일 종가 평가 → 지표 예열 → 오늘 봉 채우기. `engine.seed_bar` 가 엔진의 날짜를 오늘로 넘기므로 전일 종가 평가는 반드시 그 앞이어야 한다(뒤바뀌면 장중 재시작 때 일일 손실 한도가 오발동한다).
- 보조 작업(소식 등)의 예외가 매매를 멈추게 하지 않는다(`supervisor._aux`).
- 봉 재생(`backtest.run_bars`)은 실매매와 같은 조건이어야 한다: 긴 봉은 다음 봉 시가에 체결, 봉 안 순서는 시가→저가→고가→저가→종가, 일봉 맥락은 날마다 채운다. 학습(`Learner._bt`)도 이 경로만 쓴다.
- Claude 소식 판정은 '제목×종목' 단위로 묻고 그 종목에만 적용한다. 청산 지시(`exit_now`)는 보유 종목에만 건다. 소식 제목은 외부 입력이다 — 청산은 가격이 실제로 내려 있을 때만(`news.exit_min_drop_pct`), 하루 지시 수에는 상한(`max_exits_per_day`)이 있다. 이 안전장치를 없애지 말 것.
- 엔진·집행기의 위험 이벤트(`emit("risk", …)`)와 `alert=1` 이벤트는 총괄이 텔레그램으로 넘긴다(`_forward_alerts`). 알림을 직접 보내는 곳은 이벤트에 `sent=1` 을 붙인다.
- 전략·기본값을 바꾸면 `research`로 실제 시세 결과를 다시 확인하고 README 표를 고친다(현재 기준: 스윙 5년 +24.55%, 낙폭 7.1%, 217건).

## 아직 검증 안 된 것
- 실제 KIS 서버 호출(국내·해외 모두 가짜 서버 테스트만), 실제 Claude API 호출(가짜 클라이언트만), 텔레그램 발송, deploy/ 파일.
- 실제 서버에서만 알 수 있는 것: 취소·소멸된 주문이 주문 내역에 어떻게 보이는지, 주문번호가 날마다 다시 쓰이는지, 해외 주문 내역의 날짜·시각 필드가 현지 시각인지 한국 시각인지(`kis_overseas._row_span` 은 두 해석을 구간으로 다룬다). 모의투자에서 STUCK 이 잦으면 여기부터 볼 것.
- 주문 보조 모드는 실제 야후 시세·RSS로 화면까지 짧게 확인했을 뿐 장시간 운용은 해 보지 않았다.
