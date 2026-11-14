#!/bin/bash
# 더블클릭으로 실행(맥·리눅스). 처음 한 번은 필요한 프로그램을 설치한다.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "[처음 실행] 필요한 프로그램을 설치합니다. 몇 분 걸릴 수 있습니다..."
  python3 -m venv .venv && .venv/bin/python -m pip install -q -e . || { echo "설치 실패 — Python 3.11 이상이 필요합니다."; read -r; exit 1; }
fi
echo
echo " 1. 연습 (합성 시세, 돈이 들지 않음)"
echo " 2. 주문 보조 (실제 시세로 판단, 주문은 내가 직접)  [기본]"
echo " 3. 한국투자증권 모의투자 (자동 주문, .env 설정 필요)"
read -r -p "번호를 고르고 Enter (그냥 Enter = 2): " c
case "$c" in 1) MODE=sim ;; 3) MODE=kis-mock ;; *) MODE=manual ;; esac
echo "화면: http://127.0.0.1:8787   (끄려면 Ctrl+C)"
( sleep 4; (open http://127.0.0.1:8787 || xdg-open http://127.0.0.1:8787) >/dev/null 2>&1 ) &
exec .venv/bin/python -m aitrader run --mode "$MODE"
