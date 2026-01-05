# ========================================
# 合约价格异动监控系统 Docker 镜像
# ========================================

FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY config/ ./config/
COPY src/ ./src/
COPY main.py .

# 创建日志目录
RUN mkdir -p logs

# 健康检查（检查进程是否存活）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

# 启动命令
CMD ["python", "main.py"]
