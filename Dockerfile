# 公网部署用的镜像
# 构建：docker build -t api-test-runner .
# 运行：docker run -p 8800:8800 -e ALLOW_PRIVATE_TARGETS=0 api-test-runner

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    ALLOW_PRIVATE_TARGETS=0 \
    HOST=0.0.0.0 \
    PORT=8800

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runner_app.py runner_core.py mock_server.py ui.html ./
COPY samples ./samples

EXPOSE 8800

# 用 shell 形式，这样平台注入的 $PORT 才能生效
CMD ["sh", "-c", "python -m uvicorn runner_app:app --host 0.0.0.0 --port ${PORT:-8800}"]
