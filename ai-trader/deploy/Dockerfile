FROM python:3.13-slim
# 로그 시각만 한국 시각으로 — 매매 일과는 거래소 현지 시각으로 자동 계산합니다
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY aitrader ./aitrader
RUN pip install --no-cache-dir .
VOLUME /app/data
EXPOSE 8787
ENTRYPOINT ["python", "-m", "aitrader"]
# 컨테이너 밖에서 화면을 보려면 0.0.0.0 으로 열어야 하고, 그러려면 DASHBOARD_TOKEN 이 필요합니다(없으면 시작을 거부).
CMD ["run", "--mode", "sim", "--host", "0.0.0.0"]
