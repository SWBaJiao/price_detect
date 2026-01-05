#!/bin/bash
# ========================================
# 合约价格异动监控系统 - 一键部署脚本
# ========================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  合约价格异动监控系统${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# 检查 Docker
check_docker() {
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}错误: Docker 未安装${NC}"
        exit 1
    fi

    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        echo -e "${RED}错误: Docker Compose 未安装${NC}"
        exit 1
    fi

    echo -e "${GREEN}✓ Docker 已安装${NC}"
}

# 检查配置
check_config() {
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}警告: .env 文件不存在，从模板创建...${NC}"
        if [ -f ".env.example" ]; then
            cp .env.example .env
            echo -e "${YELLOW}请编辑 .env 文件配置 Telegram Token 和 Chat ID${NC}"
            exit 1
        fi
    fi

    # 检查必要的环境变量
    source .env
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ "$TELEGRAM_BOT_TOKEN" = "your_bot_token_here" ]; then
        echo -e "${RED}错误: 请在 .env 中配置 TELEGRAM_BOT_TOKEN${NC}"
        exit 1
    fi

    if [ -z "$TELEGRAM_CHAT_ID" ]; then
        echo -e "${RED}错误: 请在 .env 中配置 TELEGRAM_CHAT_ID${NC}"
        exit 1
    fi

    echo -e "${GREEN}✓ 配置检查通过${NC}"
}

# 创建日志目录
create_dirs() {
    mkdir -p logs
    echo -e "${GREEN}✓ 日志目录已创建${NC}"
}

# 检查并清理已运行的容器
cleanup_existing() {
    # 检查容器是否存在（运行中或已停止）
    if docker compose ps -q 2>/dev/null | grep -q .; then
        echo -e "${YELLOW}检测到已存在的容器，正在清理...${NC}"
        docker compose down --remove-orphans 2>/dev/null || true
        echo -e "${GREEN}✓ 已清理旧容器${NC}"
    elif docker compose ps -a -q 2>/dev/null | grep -q .; then
        echo -e "${YELLOW}检测到已停止的容器，正在清理...${NC}"
        docker compose down --remove-orphans 2>/dev/null || true
        echo -e "${GREEN}✓ 已清理旧容器${NC}"
    fi
}

# 构建镜像
build() {
    echo ""
    echo -e "${YELLOW}正在构建 Docker 镜像...${NC}"
    docker compose build --no-cache
    echo -e "${GREEN}✓ 镜像构建完成${NC}"
}

# 启动服务
start() {
    echo ""
    echo -e "${YELLOW}正在启动服务...${NC}"
    docker compose up -d
    echo -e "${GREEN}✓ 服务已启动${NC}"
    echo ""
    echo -e "${GREEN}查看日志: docker compose logs -f${NC}"
}

# 停止服务
stop() {
    echo -e "${YELLOW}正在停止服务...${NC}"
    docker compose down
    echo -e "${GREEN}✓ 服务已停止${NC}"
}

# 重启服务
restart() {
    echo -e "${YELLOW}正在重启服务...${NC}"
    docker compose restart
    echo -e "${GREEN}✓ 服务已重启${NC}"
}

# 查看状态
status() {
    echo -e "${YELLOW}服务状态:${NC}"
    docker compose ps
}

# 查看日志
logs() {
    docker compose logs -f --tail=100
}

# 显示帮助
show_help() {
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start     构建并启动服务（默认）"
    echo "  stop      停止服务"
    echo "  restart   重启服务"
    echo "  status    查看服务状态"
    echo "  logs      查看实时日志"
    echo "  build     仅构建镜像"
    echo "  help      显示帮助"
}

# 主逻辑
case "${1:-start}" in
    start)
        check_docker
        check_config
        create_dirs
        cleanup_existing
        build
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    build)
        check_docker
        build
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}未知命令: $1${NC}"
        show_help
        exit 1
        ;;
esac
