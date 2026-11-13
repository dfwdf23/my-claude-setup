FROM python:3.13-slim
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY aitrader ./aitrader
RUN pip install --no-cache-dir .
VOLUME /app/data
EXPOSE 8787
ENTRYPOINT ["python", "-m", "aitrader"]
CMD ["run", "--mode", "sim", "--host", "0.0.0.0"]
