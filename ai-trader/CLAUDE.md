# ai-trader — 작업 안내 (Claude용, 짧게 유지)

AI 자동매매 시스템. 상세는 README.md. 여기에는 다음 세션이 코드를 다시 다 읽지 않도록 핵심만 적는다.

## 명령
- 테스트: `.venv/bin/python -m pytest -q` (전부 통과해야 커밋)
- 시뮬레이션: `.venv/bin/python -m aitrader run --mode sim --speed 600` → http://127.0.0.1:8787
- 지연 측정: `.venv/bin/python -m aitrader bench`

## 구조(파일 → 책임)
- `aitrader/engine.py` 빠른 경로 전체(틱→주문). **여기에 await·LLM·I/O 금지.**
- `aitrader/agents/` chart(차트분석) · guards(과매수 방지, 리스크) · trade(비중조절, 매수, 매도) · market(시장분석, 종목선정) · learner(복기)
- `aitrader/executor.py` 주문 집행 · `supervisor.py` 총괄(일과·스냅샷) · `llm.py` Claude 호출/예산
- `aitrader/brokers/` base(인터페이스) · sim(합성 시세) · kis(한국투자증권)
- `aitrader/dashboard/` server.py + static/index.html(단일 파일, 외부 CDN 없음)

## 지켜야 할 불변 조건
- 매도 주문은 어떤 한도로도 막지 않는다(위험 축소는 항상 허용).
- 학습(`params.BOUNDS`)은 전략 파라미터만 바꾼다. `RiskCfg` 한도는 학습 대상이 아니다.
- 파라미터 변경은 반드시 `Learner.validate`(기록 봉 재생)를 통과해야 채택된다.
- 실전(`kis-live`)은 `AITRADER_LIVE_CONFIRM` 없이는 시작되지 않는다. 이 가드를 약화시키지 말 것.
- Claude 호출은 `llm.LLM.ask_json` 한 곳으로만. 새 호출을 추가하면 예산·게이팅을 함께 설계한다.
- 대시보드에 표시하는 문자열은 `textContent`로만 넣는다(종목명 등은 외부 데이터).

## 아직 검증 안 된 것
- 실제 KIS 서버 호출(가짜 서버 테스트만 있음), 실제 Claude API 호출(가짜 클라이언트 테스트만 있음), deploy/ 파일.
