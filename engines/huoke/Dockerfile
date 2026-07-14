FROM python:3.13-slim

# ADB 客户端 + 基础工具
RUN apt-get update && \
    apt-get install -y --no-install-recommends android-tools-adb && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs/screenshots data

EXPOSE 8000

# 默认通过环境变量 ADB_SERVER_HOST 指向宿主机 ADB server
ENV ADB_SERVER_HOST=host.docker.internal
ENV PYTHONUNBUFFERED=1

CMD ["python", "src/main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
