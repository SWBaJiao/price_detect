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

# 配置阿里云 apt 镜像源
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# 安装系统依赖（包括中文字体和进程工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    fonts-noto-cjk \
    procps \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# 复制依赖文件并安装（使用阿里云 pip 镜像源）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

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
