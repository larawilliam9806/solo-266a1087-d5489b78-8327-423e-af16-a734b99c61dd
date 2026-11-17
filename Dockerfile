FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/catalog.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./run.py
COPY examples ./examples

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 多 worker 仍由 SQLite BEGIN IMMEDIATE + busy_timeout 保证跨进程并发安全
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "app:create_app()"]
