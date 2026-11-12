#!/bin/bash
# 맥: 이 파일을 더블클릭하면 스튜디오가 켜집니다. 처음에 막히면 README 의 "macOS 에서 막힐 때" 참고.
cd "$(dirname "$0")" || exit 1
bash scripts/start-unix.sh
status=$?
if [ $status -ne 0 ]; then echo; read -n 1 -s -r -p "문제가 생겼습니다. 아무 키나 누르면 창이 닫힙니다."; fi
exit $status
