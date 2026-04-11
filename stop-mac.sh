#!/usr/bin/env bash
# ============================================================
# ChatFlow Mac 停止脚本
# 用法：
#   chmod +x stop-mac.sh
#   ./stop-mac.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/llm-chat"
SANDBOX_DIR="$COMPOSE_DIR/sandbox"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo "============================================"
echo "  ChatFlow Stop All Services"
echo "============================================"
echo ""

# ── 检查 Docker ───────────────────────────────────────────────
if ! docker info &>/dev/null; then
    error "Docker 未运行，请先启动 Docker Desktop。"
fi

# ── 停止主服务 ────────────────────────────────────────────────
info "[1/2] 停止主服务..."
cd "$COMPOSE_DIR"
docker compose -f docker-compose.prod.yml down 2>/dev/null && info "生产服务已停止" || true
docker compose down 2>/dev/null && info "开发服务已停止" || true

# ── 停止 Sandbox ──────────────────────────────────────────────
info "[2/2] 停止 Sandbox..."
cd "$SANDBOX_DIR"
docker compose --profile cluster down 2>/dev/null || true
docker compose --profile standalone down 2>/dev/null || true
info "Sandbox 已停止"

# ── 完成 ─────────────────────────────────────────────────────
echo ""
echo "============================================"
echo -e "  ${GREEN}所有服务已停止。${NC}"
echo "============================================"
