#!/usr/bin/env bash
# ============================================================
# ChatFlow Mac Mini 生产部署脚本
# 适用平台：Apple Silicon（M4 及以上）macOS
#
# 用法：
#   chmod +x start-mac.sh
#   ./start-mac.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/llm-chat"
ENV_FILE="$COMPOSE_DIR/.env.prod"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.prod.yml"
DATA_ROOT="/opt/chatflow-data"

# ── 颜色输出 ────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo "============================================"
echo "  ChatFlow Mac Mini Production Deploy"
echo "============================================"
echo ""

# ── 依赖检查 ────────────────────────────────────────────────
info "检查依赖..."

if ! command -v docker &>/dev/null; then
    error "Docker 未安装。请先安装 Docker Desktop for Mac。"
fi

if ! docker info &>/dev/null; then
    error "Docker 未运行，请先启动 Docker Desktop。"
fi

info "Docker 版本: $(docker --version)"

# ── 检查 .env.prod ───────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    warn ".env.prod 不存在，从模板创建..."
    cp "$COMPOSE_DIR/.env.prod.example" "$ENV_FILE"
    echo ""
    echo "  ⚠️  请先编辑 $ENV_FILE"
    echo "  填入 API Key、数据库密码等实际值，然后重新运行此脚本。"
    echo ""
    open "$ENV_FILE" 2>/dev/null || true
    exit 0
fi

# ── 创建数据目录 ─────────────────────────────────────────────
info "创建数据持久化目录..."

# 从 .env.prod 读取目录配置（支持自定义路径）
source_env_var() {
    local key="$1"
    local default="$2"
    grep "^${key}=" "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "$default"
}

QDRANT_DIR="$(source_env_var QDRANT_DATA_DIR "$DATA_ROOT/qdrant")"
POSTGRES_DIR="$(source_env_var POSTGRES_DATA_DIR "$DATA_ROOT/postgres")"
REDIS_DIR="$(source_env_var REDIS_DATA_DIR "$DATA_ROOT/redis")"
LOG_DIR="$(source_env_var BACKEND_LOG_HOST_DIR "$DATA_ROOT/logs")"

for dir in "$QDRANT_DIR" "$POSTGRES_DIR" "$REDIS_DIR" "$LOG_DIR"; do
    if [[ ! -d "$dir" ]]; then
        sudo mkdir -p "$dir"
        sudo chown "$(whoami)" "$dir"
        info "创建目录: $dir"
    fi
done

# ── 拉取 / 构建镜像 ──────────────────────────────────────────
info "构建并启动生产服务..."
cd "$COMPOSE_DIR"

docker compose -f docker-compose.prod.yml --env-file .env --env-file .env.prod up -d --build

echo ""
info "等待服务健康检查..."
sleep 15

# ── 健康检查 ─────────────────────────────────────────────────
info "服务状态："
docker compose -f docker-compose.prod.yml ps

echo ""
MAX_WAIT=60
ELAPSED=0
info "等待后端就绪（最多 ${MAX_WAIT}s）..."
while [[ $ELAPSED -lt $MAX_WAIT ]]; do
    if curl -sf http://localhost/api/tools &>/dev/null; then
        echo ""
        info "后端健康检查通过 ✓"
        break
    fi
    sleep 3
    ELAPSED=$((ELAPSED + 3))
    echo -n "."
done

if [[ $ELAPSED -ge $MAX_WAIT ]]; then
    warn "后端健康检查超时，请检查日志："
    echo "  docker compose -f docker-compose.prod.yml logs -f backend"
fi

# ── 完成 ─────────────────────────────────────────────────────
echo ""
echo "============================================"
echo -e "  ${GREEN}ChatFlow 生产环境已启动！${NC}"
echo "============================================"
echo ""
echo "  访问地址："
echo "    本机:     http://localhost"
echo "    局域网:   http://$(ipconfig getifaddr en0 2>/dev/null || echo 'YOUR_IP')"
echo ""
echo "  常用命令："
echo "    查看日志: docker compose -f llm-chat/docker-compose.prod.yml logs -f backend"
echo "    停止服务: docker compose -f llm-chat/docker-compose.prod.yml down"
echo "    重启后端: docker compose -f llm-chat/docker-compose.prod.yml restart backend"
echo ""
echo "  数据目录: $DATA_ROOT"
echo "============================================"
