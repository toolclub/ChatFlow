# ── Stage 1: Build ───────────────────────────────────────────────────────────
ARG NODE_IMAGE=node:20-alpine
FROM ${NODE_IMAGE} AS builder

WORKDIR /app

# 先复制依赖文件，利用缓存
COPY package*.json ./
RUN npm ci --prefer-offline

# 复制源码并构建（跳过 tsc 类型检查，直接用 vite build）
COPY . .
RUN npx vite build

# ── Stage 2: Serve with Nginx ─────────────────────────────────────────────────
ARG NGINX_IMAGE=nginx:1.27-alpine
FROM ${NGINX_IMAGE}

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
