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
SANDBOX_DIR="$COMPOSE_DIR/sandbox"
ENV_FILE="$COMPOSE_DIR/.env.prod"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.prod.yml"
DATA_ROOT="$HOME/chatflow-data"

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

# ── 保证 base 镜像本地化 ──────────────────────────────────────
# Mac Mini 到 docker.io 网络不稳，BuildKit 查 metadata 会超时。
# 策略：把公共镜像重 tag 成 chatflow-base-*:local，再让 Dockerfile 用这个名字
# 构建。本地名在 registry 不存在 → BuildKit 查都不查 → 完全离线构建。
ensure_local_base() {
    local src="$1" dst="$2"
    if docker image inspect "$dst" &>/dev/null; then
        return 0
    fi
    if docker image inspect "$src" &>/dev/null; then
        docker tag "$src" "$dst"
        info "已打本地 tag: $src → $dst"
    else
        info "本地无 $src，首次拉取（仅此一次，网络失败可手动 docker pull 后重跑）..."
        if ! docker pull "$src"; then
            error "$src 拉取失败；请在有网环境 docker pull 后 docker save/load 过来"
        fi
        docker tag "$src" "$dst"
    fi
}

info "准备本地 base 镜像..."
ensure_local_base "python:3.12-slim"  "chatflow-base-python:local"
ensure_local_base "node:20-alpine"    "chatflow-base-node:local"
ensure_local_base "nginx:1.27-alpine" "chatflow-base-nginx:local"

export BACKEND_BASE_IMAGE="chatflow-base-python:local"
export FRONTEND_NODE_IMAGE="chatflow-base-node:local"
export FRONTEND_NGINX_IMAGE="chatflow-base-nginx:local"

# ── 创建数据目录 ─────────────────────────────────────────────
info "创建数据持久化目录..."

for dir in \
    "$DATA_ROOT/qdrant" \
    "$DATA_ROOT/postgres" \
    "$DATA_ROOT/redis" \
    "$DATA_ROOT/logs" \
    "$DATA_ROOT/secret"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        info "创建目录: $dir"
    fi
done

# ── 启动 Sandbox ─────────────────────────────────────────────
# 应用镜像每次 --build（确保代码最新），FROM 的基础镜像本地有就不重拉
info "[1/2] 启动 Sandbox..."
cd "$SANDBOX_DIR"
if docker compose --profile cluster up -d --build 2>/dev/null; then
    info "Sandbox 集群启动成功（端口 2222-2224）"
else
    warn "集群启动失败，尝试单节点..."
    if docker compose --profile standalone up -d --build; then
        info "Sandbox 单节点启动成功（端口 2222）"
    else
        warn "Sandbox 启动失败，代码执行功能将不可用"
    fi
fi
echo ""

# ── 构建并启动主服务 ────────────────────────────────────────
info "[2/2] 构建并启动生产服务..."
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
