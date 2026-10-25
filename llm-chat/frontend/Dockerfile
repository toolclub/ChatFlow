# 全局 ARG（声明在所有 FROM 之前，所有 stage 的 FROM 都能引用）
ARG NODE_IMAGE=node:20-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ── Stage 1: Build ───────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS builder

WORKDIR /app

# 先复制依赖文件，利用缓存
COPY package*.json ./
RUN npm ci --prefer-offline

# 类型检查与回归通过后才构建发布产物。
COPY . .
RUN npm test && npm run build

# ── Stage 2: Serve with Nginx ─────────────────────────────────────────────────
FROM ${NGINX_IMAGE}

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# 127.0.0.1 而非 localhost：busybox wget 会先解析到 ::1，nginx 只监听 IPv4
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
