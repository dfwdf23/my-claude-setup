@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 자동매매
if not exist ".venv\Scripts\python.exe" (
  echo [처음 실행] 필요한 프로그램을 설치합니다. 몇 분 걸릴 수 있습니다...
  py -3 -m venv .venv || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo 파이썬을 찾을 수 없습니다. https://www.python.org 에서 Python 3.11 이상을 설치한 뒤 다시 실행하세요.
    pause & exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -q -e . || (echo 설치에 실패했습니다. & pause & exit /b 1)
)
echo.
echo  1. 연습 (합성 시세, 돈이 들지 않음)
echo  2. 주문 보조 (실제 시세로 판단, 주문은 내가 직접)  [기본]
echo  3. 한국투자증권 모의투자 (자동 주문, .env 설정 필요)
echo.
set MODE=manual
set /p CHOICE=번호를 고르고 Enter (그냥 Enter = 2): 
if "%CHOICE%"=="1" set MODE=sim
if "%CHOICE%"=="3" set MODE=kis-mock
echo.
echo 화면: http://127.0.0.1:8787   (끄려면 이 창에서 Ctrl+C)
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8787"
".venv\Scripts\python.exe" -m aitrader run --mode %MODE%
pause
